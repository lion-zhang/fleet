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


def provenance(row: dict[str, Any]) -> str:
    """"via <machine>" when this row is second-hand, "" when we measured it ourselves.

    Under the access list a machine reaches only what it is granted, so the center
    relays telemetry for the rest. A relayed number is still worth showing -- it beats a
    blank row -- but it must not be displayed as though we had just taken it, for the
    same reason `staleness` exists: rendering it as live would be a lie in exactly the
    place someone would act on it.
    """
    if (row.get("source") or "self") == "self":
        return ""
    by = row.get("probed_by") or "center"
    return f"via {by}"


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


# Ceiling on the backoff exponent. `2 ** fails` is an unbounded Python int, but `base`
# is a float (cmd_top's --interval is a Typer float option), and multiplying a float by
# an int larger than DBL_MAX raises OverflowError rather than returning inf. A device
# that stays unreachable keeps incrementing at one failure per max_backoff, so it
# crosses 2 ** 1024 in about 17 hours -- precisely the host-down-overnight case the
# ceiling exists to serve. Doubling 64 times already dwarfs any sane max_backoff, so
# clamping here changes no reachable answer.
_MAX_DOUBLINGS = 64


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
        return min(self.max_backoff, base * (2 ** min(fails, _MAX_DOUBLINGS)))

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


def _model(name: str) -> str:
    """The card, without the vendor boilerplate that widens the column.

    Only "NVIDIA GeForce " used to be stripped, so every datacentre card kept a
    "NVIDIA " that says nothing -- and those are exactly the machines with enough cards
    for the space to matter.
    """
    for prefix in ("NVIDIA GeForce ", "NVIDIA "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _gb(gb: float) -> str:
    """Free space at a glance: "1648G" is arithmetic, "1.6T" is the answer."""
    return f"{gb / 1024:.1f}T" if gb >= 1024 else f"{gb:.0f}G"


def _models(gpus: list[dict[str, Any]]) -> str:
    return "\n".join(_model(g.get("name", "")) for g in gpus)


def gpu_cells(row: dict[str, Any]) -> tuple[str, str, str]:
    """`top`'s three GPU columns -- model, utilisation, VRAM -- one line per card.

    Per card rather than "RTX 4090 x2", which named a heterogeneous pair after whichever
    card happened to be first. The VRAM cell was worse than terse: it drew one bar over
    the *sum* of every card's memory and printed the *largest single card's* free figure
    beside it, so the meter and the number described different hardware. One line each
    and they describe the same card.
    """
    gpus = row.get("gpus") or []
    if not gpus:
        return "-", "-", "-"
    utils = "\n".join(f"{g.get('util_pct') or 0}%" for g in gpus)
    vram = "\n".join(
        escape(meter(g.get("vram_total_mib", 0) - g.get("vram_free_mib", 0),
                     g.get("vram_total_mib", 0), 6))
        + f" {g.get('vram_free_mib', 0) / 1024:.1f}G"
        for g in gpus)
    return _models(gpus), utils, vram


def gpu_cells_compact(row: dict[str, Any]) -> tuple[str, str]:
    """`ls`'s two GPU columns: the model, and free VRAM tagged busy or idle.

    ls answers "can I claim this card", so it reports a verdict where `top` draws a
    meter. Per card also fixes the verdict itself: it used to read `busy` when *any*
    card was, so a box with one saturated card and three idle ones looked unusable.
    """
    gpus = row.get("gpus") or []
    if not gpus:
        return "-", "-"
    vram = "\n".join(
        f"{g.get('vram_free_mib', 0) / 1024:.1f}G "
        + ("[red]busy[/red]" if g.get("busy") else "[green]idle[/green]")
        for g in gpus)
    return _models(gpus), vram


def disk_cell(row: dict[str, Any]) -> str:
    """Free space, one line per mount.

    A rental whose `/` is a nearly full container overlay and whose real storage sits on
    /workspace reported only the roomiest of the two -- true, and useless, because the
    mount that will end a long job is precisely the one that got hidden.

    Rows built at Detail.COMPACT (`fleet ls`) carry no per-mount list, only the single
    reduced figure, and fall back to it.
    """
    disks = row.get("disks")
    if not disks:
        free = row.get("disk_free_gb")
        if not free:
            return "-"
        mount = row.get("disk_mount")
        # name the mount unless it is root: "1.6T" alone is misleading on a rental
        # whose / is a small overlay and whose real storage lives elsewhere.
        return _gb(free) if mount == "/" else f"{_gb(free)} [dim]{mount}[/dim]"
    if len(disks) == 1 and disks[0].get("mount") == "/":
        return _gb(disks[0].get("free_gb", 0))
    return "\n".join(_gb(d.get("free_gb", 0)).rjust(5)
                      + f" [dim]{d.get('mount', '')}[/dim]" for d in disks)


def device_lines(row: dict[str, Any]) -> int:
    """How many lines this device's tallest column needs."""
    return max(len(row.get("gpus") or []), len(row.get("disks") or []), 1)


def name_cell(row: dict[str, Any]) -> str:
    """The device name, with a dim gutter continuing it down a multi-line row.

    Without it a trailing card or mount floats with nothing tying it to the machine
    above. One quiet mark in one column answers that: a glyph in every multi-line column
    repeats the noise per column, and a rule between devices spends a whole row per
    machine in a view whose point is fitting the fleet on one screen.
    """
    name = f"[bold]{row['name']}[/bold]" + (" [dim]\u2190[/dim]" if row.get("is_self") else "")
    return name + "\n[dim]\u2502[/dim]" * (device_lines(row) - 1)


def render_fleet(rows: list[dict[str, Any]], summary: dict[str, Any],
                 live_within: int = 10) -> Table:
    t = Table(box=None, pad_edge=False, header_style="bold", expand=False)
    # A right-justified DISK FREE pads the short lines of a multi-mount cell from the
    # left and comes out ragged, so the column flips left only once some device really
    # has more than one card or mount. A fleet of plain boxes renders as it always did.
    tall = any(device_lines(r) > 1 for r in rows)
    for col, kw in (("", {}), ("NAME", {"no_wrap": True}),
                    # "RTX PRO 6000 Blackwell Workstation Edition" is 41 characters of
                    # column; stripping the vendor prefix is not enough to stop a name
                    # like that pushing every number off a narrow terminal.
                    ("GPU", {"no_wrap": True, "overflow": "ellipsis", "max_width": 24}),
                    ("GPU%", {"justify": "right", "no_wrap": True}),
                    ("VRAM FREE", {"no_wrap": True}),
                    ("CPU", {"justify": "right", "no_wrap": True}),
                    ("RAM FREE", {"justify": "right", "no_wrap": True}),
                    ("DISK FREE", {"justify": "left" if tall else "right",
                                   "no_wrap": True}),
                    ("AGE", {"justify": "right", "no_wrap": True}),
                    ("NOTE", {"no_wrap": True, "overflow": "ellipsis", "max_width": 34})):
        t.add_column(col, **kw)
    for row in rows:
        gpu, util, vram = gpu_cells(row)
        pct = cpu_pct(row)
        stale = staleness(row, live_within)
        note = (row.get("error", {}).get("detail", "") if row["status"] != "ok"
                else (row["alerts"][0] if row.get("alerts") else ""))
        t.add_row(
            _DOT.get(row["status"], "?"),
            name_cell(row),
            gpu,
            util,
            vram,
            "?" if pct is None else f"{pct}%",
            f"{row['ram_free_gb']:.0f}G" if row.get("ram_free_gb") else "-",
            disk_cell(row),
            f"[dim]{stale}[/dim]" if stale else "[green]live[/green]",
            " · ".join(x for x in (provenance(row), note) if x),
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
