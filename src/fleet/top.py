"""Live view of the fleet.

Everything that decides what is shown and when to re-probe lives here as pure
functions; the Live loop in cli.py is a thin shell around them. That split is what makes
the interesting behaviour testable without a terminal.

The scheduling half matters more than the rendering half. A view that polled every
device every two seconds would hammer the multi-user cluster config.py explicitly
promises not to hammer, and would redial a dead host hundreds of times an hour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.markup import escape
from rich.table import Table

from .models import Device, Kind

# Block characters, not "#": rich parses a leading "#" inside brackets as a hex colour
# tag and silently eats the whole bar. Every meter is also escape()d at the render site,
# so this cannot come back if the characters change again.
FILLED, EMPTY = "█", "░"


def meter(used: float, total: float, width: int = 10) -> str:
    """A fixed-width bar. Never divides by zero and never overflows its width.

    Both matter in the render loop: a device with no GPU reports totals of zero, some
    drivers report usage above the total, and either one shifting or crashing a row
    takes the whole view with it.
    """
    if total <= 0:
        filled = 0
    else:
        filled = round(width * max(0.0, min(1.0, used / total)))
    return "[" + FILLED * filled + EMPTY * (width - filled) + "]"


def cpu_pct(row: dict[str, Any]) -> int | None:
    """Load average over core count, the usual approximation.

    None rather than 0 when either is missing: "idle" and "we do not know" are
    different claims, and only one of them should make you launch a job here.
    """
    load, cores = row.get("load"), row.get("cpu_cores")
    if not load or not cores:
        return None
    return min(100, round(100 * float(load[0]) / float(cores)))


def gpu_pct(row: dict[str, Any]) -> int | None:
    """Utilisation of the busiest card, or None when there is no GPU at all.

    The busiest rather than the average: one saturated card makes the box a bad place
    to send work however idle its siblings are, which is why free_vram_mib already
    takes the max too. None rather than 0 because "no GPU" and "an idle GPU" are
    different machines, and only one of them is worth waiting for.

    This is compute, not memory. A card can sit at 100% with VRAM to spare, or hold
    VRAM while doing nothing -- reading only the memory meter answers the wrong
    question.
    """
    gpus = row.get("gpus") or []
    if not gpus:
        return None
    return max(int(g.get("util_pct") or 0) for g in gpus)


def staleness(row: dict[str, Any], live_within: int) -> str:
    """How out of date this row is, when that is worth saying.

    A shared host is polled every few minutes. Rendering its number as though it were
    live would be a lie in exactly the place someone would act on it.
    """
    age = row.get("telemetry_age_s")
    if age is None:
        return "?"
    if age <= live_within:
        return ""
    if age < 60:
        return f"{int(age)}s"
    if age < 3600:
        return f"{int(age // 60)}m"
    return f"{int(age // 3600)}h"


@dataclass
class Schedule:
    """Decides which devices are due for a re-probe.

    Shared hosts get their own slow cadence, and anything failing backs off
    exponentially up to a ceiling -- without the ceiling a host that was down overnight
    would never be retried once it came back.
    """

    interval: float = 2.0
    shared_interval: float = 300.0
    max_backoff: float = 60.0
    _last: dict[str, float] = field(default_factory=dict)
    _fails: dict[str, int] = field(default_factory=dict)

    def _wait_for(self, dev: Device) -> float:
        base = self.shared_interval if dev.kind is Kind.SHARED else self.interval
        fails = self._fails.get(dev.id, 0)
        if not fails:
            return base
        return min(self.max_backoff, base * (2 ** fails))

    def due(self, devices: list[Device], now: float) -> list[Device]:
        out = []
        for dev in devices:
            last = self._last.get(dev.id)
            if last is None or now - last >= self._wait_for(dev):
                out.append(dev)
        return out

    def record(self, device_id: str, *, ok: bool, now: float) -> None:
        self._last[device_id] = now
        if ok:
            self._fails.pop(device_id, None)
        else:
            self._fails[device_id] = self._fails.get(device_id, 0) + 1


_DOT = {"ok": "[green]●[/green]", "auth_failed": "[yellow]◐[/yellow]",
        "timeout": "[dim]○[/dim]", "refused": "[red]○[/red]", "closed": "[red]○[/red]",
        "unreachable": "[dim]○[/dim]", "host_key_mismatch": "[yellow]◐[/yellow]",
        "probe_error": "[yellow]◐[/yellow]", "unknown": "[dim]?[/dim]"}


def _gpu_cell(row: dict[str, Any]) -> tuple[str, str]:
    gpus = row.get("gpus") or []
    if not gpus:
        return "-", ""
    name = gpus[0].get("name", "").replace("NVIDIA GeForce ", "")
    if len(gpus) > 1:
        name += f" x{len(gpus)}"
    used = sum(g.get("vram_total_mib", 0) - g.get("vram_free_mib", 0) for g in gpus)
    total = sum(g.get("vram_total_mib", 0) for g in gpus)
    free_gb = max((g.get("vram_free_mib", 0) for g in gpus), default=0) / 1024
    return name, f"{escape(meter(used, total, 6))} {free_gb:.1f}G"


def render_fleet(rows: list[dict[str, Any]], summary: dict[str, Any],
                 live_within: int = 10) -> Table:
    t = Table(box=None, pad_edge=False, header_style="bold", expand=False)
    for col, kw in (("", {}), ("NAME", {"no_wrap": True}), ("GPU", {"no_wrap": True}),
                    ("GPU%", {"justify": "right", "no_wrap": True}),
                    ("VRAM FREE", {"no_wrap": True}),
                    ("CPU", {"justify": "right", "no_wrap": True}),
                    ("RAM FREE", {"justify": "right", "no_wrap": True}),
                    ("DISK FREE", {"justify": "right", "no_wrap": True}),
                    ("AGE", {"justify": "right", "no_wrap": True}),
                    ("NOTE", {"no_wrap": True, "overflow": "ellipsis", "max_width": 34})):
        t.add_column(col, **kw)
    for row in rows:
        gpu, vram = _gpu_cell(row)
        pct = cpu_pct(row)
        util = gpu_pct(row)
        stale = staleness(row, live_within)
        note = (row.get("error", {}).get("detail", "") if row["status"] != "ok"
                else (row["alerts"][0] if row.get("alerts") else ""))
        t.add_row(
            _DOT.get(row["status"], "?"),
            f"[bold]{row['name']}[/bold]",
            gpu,
            "-" if util is None else f"{util}%",
            vram,
            "?" if pct is None else f"{pct}%",
            f"{row['ram_free_gb']:.0f}G" if row.get("ram_free_gb") else "-",
            f"{row['disk_free_gb']:.0f}G" if row.get("disk_free_gb") else "-",
            f"[dim]{stale}[/dim]" if stale else "[green]live[/green]",
            note,
        )
    return t


def render_device(row: dict[str, Any], live_within: int = 10) -> Table:
    """One device in detail: every GPU, then what is actually holding it."""
    t = Table(box=None, pad_edge=False, show_header=False)
    t.add_column(no_wrap=True)
    t.add_column(overflow="ellipsis")
    stale = staleness(row, live_within)
    t.add_row("[bold]host[/bold]",
              f"{row.get('hostname') or row['name']}  [dim]{row.get('os', '')}[/dim]  "
              + (f"[dim]{stale} old[/dim]" if stale else "[green]live[/green]"))
    pct = cpu_pct(row)
    t.add_row("[bold]cpu[/bold]",
              (f"{escape(meter(pct or 0, 100, 20))} {pct}%" if pct is not None else "?")
              + f"  [dim]{row.get('cpu_cores') or '?'} cores[/dim]")
    total = row.get("ram_total_gb") or 0
    free = row.get("ram_free_gb") or 0
    t.add_row("[bold]ram[/bold]",
              f"{escape(meter(total - free, total, 20))} {free:.0f}G free")
    for gpu in row.get("gpus") or []:
        used = gpu.get("vram_total_mib", 0) - gpu.get("vram_free_mib", 0)
        t.add_row(f"[bold]gpu{gpu.get('idx', 0)}[/bold]",
                  f"{escape(meter(used, gpu.get('vram_total_mib', 0), 20))} "
                  f"{gpu.get('vram_free_mib', 0) / 1024:.1f}G free  "
                  f"[dim]{gpu.get('util_pct', 0)}% util  {gpu.get('name', '')}[/dim]")
    for disk in row.get("disks") or []:
        t.add_row("[bold]disk[/bold]",
                  f"{escape(meter(disk.get('use_pct', 0), 100, 20))} "
                  f"{disk.get('free_gb', 0):.0f}G free  [dim]{disk.get('mount', '')}[/dim]")
    for proc in (row.get("processes") or [])[:8]:
        t.add_row("[dim]gpu proc[/dim]",
                  f"{proc.get('vram_mib', 0)} MiB  [dim]{proc.get('comm', '')}[/dim]")
    for proc in (row.get("top_cpu") or [])[:5]:
        t.add_row("[dim]cpu proc[/dim]",
                  f"{proc.get('cpu_pct', 0):.0f}%  [dim]{proc.get('comm', '')}[/dim]")
    for alert in row.get("alerts") or []:
        t.add_row("[yellow]![/yellow]", f"[yellow]{alert}[/yellow]")
    return t
