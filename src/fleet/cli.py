"""fleet CLI. Every read command supports --json, because the CLI -- not MCP -- is the
universal interface: cron jobs, Makefiles, and non-MCP agents can all use it."""

from __future__ import annotations

import json as jsonlib
import sys

import typer
from rich.console import Console
from rich.table import Table

from . import inventory as inv
from . import store
from .config import DB_PATH, INVENTORY_PATH, load_config
from .models import Kind, Status
from .onboard import onboard
from .probe.runner import probe_many, run_probe
from .sshcmd import resolve_command
from .view import Detail, device_view, fleet_view

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Personal compute inventory, service registry, and resource broker.")
console = Console()
err = Console(stderr=True)

_DOT = {"ok": "[green]●[/green]", "auth_failed": "[yellow]◐[/yellow]",
        "timeout": "[dim]○[/dim]", "refused": "[red]○[/red]", "closed": "[red]○[/red]",
        "unreachable": "[dim]○[/dim]", "host_key_mismatch": "[yellow]◐[/yellow]",
        "probe_error": "[yellow]◐[/yellow]", "unknown": "[dim]?[/dim]"}


def _emit(payload, as_json: bool) -> bool:
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))
    return as_json


def _rows(names: list[str] | None = None, *, refresh: bool = False,
          detail: Detail = Detail.COMPACT) -> list[dict]:
    cfg = load_config()
    devices = inv.load()
    if names:
        devices = [d for d in devices if d.name in names or d.id in names]
    conn = store.connect()

    stale = []
    for d in devices:
        st, _ = store.latest(conn, d.id)
        eligible = d.probeable or (bool(names) and d.probe_policy != "never")
        if eligible and (refresh or not store.is_fresh(st, int(cfg.telemetry_ttl_s))):
            stale.append(d)
    if stale:
        jobs = {d.id: inv.endpoints_of(d) for d in stale}
        modes = {d.id: d.probe_mode for d in stale}
        # probe_many takes one mode; group by mode so a shared host gets the polite probe
        for mode in set(modes.values()):
            subset = {k: v for k, v in jobs.items() if modes[k] == mode}
            results = probe_many(subset, mode=mode, timeout=float(cfg.probe_timeout_s),
                                 connect_timeout=int(cfg.connect_timeout_s),
                                 max_workers=int(cfg.max_workers))
            for dev_id, res in results.items():
                store.record(conn, dev_id, res)

    out = []
    for d in devices:
        st, sn = store.latest(conn, d.id)
        out.append(device_view(d, st, sn, detail))
    conn.close()
    return out


@app.command("ls")
def cmd_ls(json_out: bool = typer.Option(False, "--json"),
           refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe"),
           online: bool = typer.Option(False, "--online", help="only reachable devices")):
    """List every device with live resource availability."""
    rows = _rows(refresh=refresh)
    if online:
        rows = [r for r in rows if r["status"] == "ok"]
    view = fleet_view(rows)
    if _emit(view, json_out):
        return
    if not rows:
        err.print("[yellow]No devices yet.[/yellow]  Add one:  "
                  "[bold]fleet add \"ssh user@host\"[/bold]")
        raise typer.Exit(0)

    t = Table(box=None, pad_edge=False, header_style="bold")
    for col, kw in (("", {}), ("NAME", {"no_wrap": True}), ("KIND", {"no_wrap": True}),
                    ("GPU", {"no_wrap": True}),
                    ("VRAM FREE", {"justify": "right", "no_wrap": True}),
                    ("CPU", {"justify": "right", "no_wrap": True}),
                    ("RAM FREE", {"justify": "right", "no_wrap": True}),
                    ("$/HR", {"justify": "right", "no_wrap": True}),
                    ("AGE", {"justify": "right", "no_wrap": True}),
                    ("NOTE", {"no_wrap": True, "overflow": "ellipsis", "max_width": 42})):
        t.add_column(col, **kw)
    for r in rows:
        gpu = r["gpus"][0]["name"].replace("NVIDIA GeForce ", "") if r["gpus"] else "-"
        if r["gpu_count"] > 1:
            gpu += f" x{r['gpu_count']}"
        vram = f"{r['free_vram_mib']/1024:.1f}G" if r["free_vram_mib"] else "-"
        if r["gpus"]:
            vram += " [red]busy[/red]" if any(g["busy"] for g in r["gpus"]) else " [green]idle[/green]"
        note = r.get("error", {}).get("detail", "") if r["status"] != "ok" else (
            r["alerts"][0] if r["alerts"] else "")
        age = f"{r['telemetry_age_s']}s" if r["telemetry_age_s"] is not None else "-"
        t.add_row(_DOT.get(r["status"], "?"), f"[bold]{r['name']}[/bold]",
                  r["kind"], gpu, vram,
                  str(r["cpu_cores"] or "-"),
                  f"{r['ram_free_gb']:.0f}G" if r["ram_free_gb"] else "-",
                  f"${r['usd_per_hour']:.2f}" if r["usd_per_hour"] else "-",
                  age, note)
    console.print(t)
    s = view["summary"]
    console.print(f"\n[dim]{s['online']}/{s['total']} online · {s['gpus_free']} free GPU(s)"
                  + (f" · ${s['hourly_burn']:.2f}/hr burning" if s["hourly_burn"] else "") + "[/dim]")


@app.command("show")
def cmd_show(name: str, json_out: bool = typer.Option(False, "--json"),
             refresh: bool = typer.Option(True, "--refresh/--no-refresh")):
    """Full detail for one device."""
    rows = _rows([name], refresh=refresh, detail=Detail.FULL)
    if not rows:
        err.print(f"[red]No device named {name!r}.[/red]  Try [bold]fleet ls[/bold]")
        raise typer.Exit(1)
    r = rows[0]
    if _emit(r, json_out):
        return
    console.print(f"\n{_DOT.get(r['status'],'?')} [bold]{r['name']}[/bold]  "
                  f"[dim]{r['kind']} · {r['status']} · {r.get('os') or '?'} · {r.get('arch') or ''}[/dim]")
    if r.get("error"):
        console.print(f"  [red]{r['error']['class']}[/red]: {r['error']['detail']}")
    if r.get("cpu_model"):
        console.print(f"  cpu   {r['cpu_cores']}x {r['cpu_model']}   load {r.get('load')}")
    if r.get("ram_total_gb"):
        console.print(f"  ram   {r['ram_free_gb']:.1f} / {r['ram_total_gb']:.1f} GB free")
    for g in r["gpus"]:
        bar = "█" * (g["util_pct"] // 10) + "░" * (10 - g["util_pct"] // 10)
        console.print(f"  gpu{g['idx']}  {g['name']}  {bar} {g['util_pct']}%   "
                      f"{g['vram_free_mib']/1024:.1f}/{g['vram_total_mib']/1024:.1f} GB free")
        if g["display_used_mib"]:
            console.print(f"        [dim]{g['display_used_mib']} MiB desktop overhead (not a job)[/dim]")
        if g["unattributed_mib"]:
            console.print(f"        [yellow]{g['unattributed_mib']} MiB unattributed[/yellow]")
    for d in r.get("disks", []):
        console.print(f"  disk  {d['mount']:<12} {d['avail_kb']/1048576:.0f} GB free")
    if r.get("processes"):
        console.print("  [bold]gpu compute[/bold]")
        for p in r["processes"]:
            console.print(f"        {p['pid']:>8} {p['user']:<10} {p['vram_mib']:>6} MiB  {p['comm']}")
    elif r["gpus"]:
        console.print("  [dim]gpu compute   none — no job is using this GPU[/dim]")
    if r.get("top_cpu"):
        console.print("  [bold]top cpu[/bold]")
        for p in r["top_cpu"]:
            console.print(f"        {p['pid']:>8} {p['user']:<10} {p['cpu_pct']:>5}%  {p['comm']}")
    if r.get("services_detail"):
        console.print("  [bold]services[/bold]")
        for s in r["services_detail"]:
            console.print(f"        :{s['port']:<6} {s.get('kind','?'):<10} {s.get('comm','')}")
    for a in r["alerts"]:
        console.print(f"  [yellow]![/yellow] {a}")
    c = r["connect"]
    console.print(f"\n  [bold]connect[/bold]  {c.get('ssh_command') or c.get('hint')}")
    if r.get("notes"):
        console.print(f"  [dim]{r['notes'].strip()}[/dim]")


@app.command("add")
def cmd_add(ssh_command: str = typer.Argument(..., help='e.g. "ssh -p 58418 root@1.2.3.4"'),
            name: str = typer.Option(None, "--name"),
            kind: str = typer.Option(None, "--kind", help="permanent|rental|shared|appliance|mobile"),
            json_out: bool = typer.Option(False, "--json"),
            dry_run: bool = typer.Option(False, "--dry-run")):
    """Add a device from a pasted ssh command."""
    devices = inv.load()
    dev, res = onboard(ssh_command, name=name, kind=kind,
                       taken_names={d.name for d in devices})
    if dry_run:
        _emit({"device": dev.name, "id": dev.id, "kind": dev.kind.value,
               "status": res.status.value}, True)
        return
    devices, action = inv.upsert(devices, dev)
    inv.save(devices)
    if res.snapshot is not None or not res.ok:
        conn = store.connect()
        store.record(conn, dev.id, res)
        conn.close()
    if _emit({"action": action, "name": dev.name, "id": dev.id,
              "kind": dev.kind.value, "status": res.status.value}, json_out):
        return
    if action == "endpoint_added":
        console.print(f"[green]✓[/green] {dev.name} was already known — added another endpoint "
                      "(same machine-id, so this is one device, not two).")
    elif action == "unchanged":
        console.print(f"[dim]· {dev.name} already recorded with this endpoint.[/dim]")
    else:
        console.print(f"[green]✓[/green] added [bold]{dev.name}[/bold] "
                      f"({dev.kind.value}, {res.status.value})")
    if not res.ok:
        console.print(f"  [yellow]{res.status.value}[/yellow]: {res.error_detail}")
        console.print("  [dim]Recorded anyway and flagged needs_review.[/dim]")


@app.command("rm")
def cmd_rm(name: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Remove a device from the inventory."""
    devices = inv.load()
    dev = inv.find(devices, name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Remove {dev.name} ({dev.kind.value})?"):
        raise typer.Exit(1)
    inv.save([d for d in devices if d.id != dev.id])
    console.print(f"[green]✓[/green] removed {dev.name}")


@app.command("refresh")
def cmd_refresh(names: list[str] = typer.Argument(None), json_out: bool = typer.Option(False, "--json")):
    """Force a live probe of some or all devices."""
    rows = _rows(list(names) if names else None, refresh=True)
    if _emit(fleet_view(rows), json_out):
        return
    for r in rows:
        console.print(f"{_DOT.get(r['status'],'?')} {r['name']:<16} {r['status']:<16} "
                      f"{r['telemetry_age_s']}s ago")


@app.command("probe")
def cmd_probe(name: str, raw: bool = typer.Option(False, "--raw", help="print payload stdout")):
    """Probe one device directly. --raw captures a new parser test fixture."""
    dev = inv.find(inv.load(), name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)
    eps = inv.endpoints_of(dev)
    if raw:
        import subprocess
        from .probe.runner import PAYLOAD
        from .sshcmd import build_argv
        argv = build_argv(sorted(eps, key=lambda e: e.preference)[0],
                          remote="sh -s", env={"FLEET_MODE": dev.probe_mode})
        p = subprocess.run(argv, input=PAYLOAD.read_text(), capture_output=True, text=True)
        sys.stdout.write(p.stdout)
        raise typer.Exit(0 if p.returncode == 0 else 1)
    res = run_probe(sorted(eps, key=lambda e: e.preference)[0], mode=dev.probe_mode)
    console.print_json(jsonlib.dumps(
        {"status": res.status.value, "latency_ms": res.latency_ms,
         "error": res.error_detail, "snapshot": res.snapshot.to_dict() if res.snapshot else None},
        default=str))


@app.command("ssh", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def cmd_ssh(ctx: typer.Context, name: str):
    """Open a shell on a device, or run a command: `fleet ssh lin-xps -- nvidia-smi`.

    This exists so credentials never have to reach an agent: the wrapper resolves the
    endpoint and connects, rather than handing out a connection string plus a password.
    """
    import os
    dev = inv.find(inv.load(), name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)
    eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
    if not eps:
        err.print(f"[red]{dev.name} has no endpoint recorded[/red]")
        raise typer.Exit(1)
    ep = eps[0]
    if dev.auth_state == "needs_credentials":
        err.print(f"[yellow]{dev.name} has no working key.[/yellow] Password auth is not "
                  "implemented yet (v0.4). Install your key instead:")
        err.print(f"  [bold]ssh-copy-id -i ~/.ssh/id_ed25519.pub "
                  f"{ep.user}@{ep.target}[/bold]" + (f" -p {ep.port}" if ep.port != 22 else ""))
        raise typer.Exit(2)
    argv = ["ssh"]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.identity:
        argv += ["-i", ep.identity]
    if ep.jump:
        argv += ["-J", ep.jump]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    extra = [a for a in ctx.args if a != "--"]
    argv += extra
    os.execvp("ssh", argv)      # replace this process; ssh owns the tty from here


@app.command("paths")
def cmd_paths():
    """Show where fleet keeps its state."""
    console.print(f"inventory  {INVENTORY_PATH}")
    console.print(f"cache      {DB_PATH}   [dim](disposable — delete and re-probe)[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
