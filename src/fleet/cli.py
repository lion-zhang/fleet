"""fleet CLI. Every read command supports --json, because the CLI -- not MCP -- is the
universal interface: cron jobs, Makefiles, and non-MCP agents can all use it."""

from __future__ import annotations

import getpass
import json as jsonlib
import re
import subprocess
import sys
import time
from pathlib import Path

from contextlib import suppress
from dataclasses import replace

import typer
import yaml
from contextlib import contextmanager
from functools import lru_cache

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from . import inventory as inv
from . import store
from .config import DB_PATH, FLEET_KEY, INVENTORY_PATH, load_config
from .edit import apply_edits
from .install import build_install_argv, install_script
from .keys import ensure_keypair, install_key
from .models import Device, Kind, Status
from .onboard import onboard, onboard_self
from .probe.runner import (probe_env, probe_many, run_probe, run_probe_local)
from .setup import TARGETS, detect_targets, fleet_command, install, uninstall
from .sshcmd import build_argv, remote_command, resolve_command
from .top import (Schedule, device_lines, disk_cell, gpu_cells_compact,
                  name_cell, render_device, render_fleet)
from .view import Detail, auth_of, device_view, fleet_view

app = typer.Typer(
    add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
    help="Personal compute inventory, service registry, and resource broker.",)
console = Console()
err = Console(stderr=True)

_DOT = {"ok": "[green]●[/green]", "auth_failed": "[yellow]◐[/yellow]",
        "timeout": "[dim]○[/dim]", "refused": "[red]○[/red]", "closed": "[red]○[/red]",
        "unreachable": "[dim]○[/dim]", "host_key_mismatch": "[yellow]◐[/yellow]",
        "probe_error": "[yellow]◐[/yellow]", "unknown": "[dim]?[/dim]"}


def _this_machine(devices, what: str):
    """The device record for the machine we are on, or a useful error.

    Lets a name be omitted where "the one I am standing on" is the obvious default. Not
    offered everywhere: `fleet ssh` to yourself is what a terminal already is, and a
    destructive command must never guess which machine it is about.
    """
    me = inv.find(devices, local_device_id()) if local_device_id() else None
    if me is None:
        err.print(f"[red]This machine is not in the inventory,[/red] so there is nothing "
                  f"to {what}.")
        err.print("  [dim]add it with [bold]fleet add --self[/bold][/dim]")
        raise typer.Exit(2)
    return me


def _emit(payload, as_json: bool) -> bool:
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))
    return as_json


def _rows(names: list[str] | None = None, *, refresh: bool = False,
          detail: Detail = Detail.COMPACT) -> list[dict]:
    cfg = load_config()
    devices = inv.live(inv.load())
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
        # probe_many applies one set of options to a whole batch, so devices are grouped
        # by everything that varies per device: mode (a shared host gets the polite
        # probe) and disk paths (one device's paths must not leak into another's probe).
        me = local_device_id()
        groups: dict[tuple[str, tuple[str, ...]], dict[str, list]] = {}
        for d in stale:
            if me and d.id == me:
                # no ssh, no key, no network to look at the machine we are running on
                store.record(conn, d.id, run_probe_local(mode=d.probe_mode,
                                                         disk_paths=d.disk_paths))
                continue
            key = (d.probe_mode, tuple(d.disk_paths))
            groups.setdefault(key, {})[d.id] = inv.endpoints_of(d)
        for (mode, paths), subset in groups.items():
            results = probe_many(subset, mode=mode, disk_paths=list(paths),
                                 timeout=float(cfg.probe_timeout_s),
                                 connect_timeout=int(cfg.connect_timeout_s),
                                 max_workers=int(cfg.max_workers))
            for dev_id, res in results.items():
                store.record(conn, dev_id, res)
        # A host that only accepts a password stays auth_failed forever otherwise. This
        # is a fallback rather than part of the sweep: the fan-out stays untouched, and
        # only the handful that actually failed pay for a second, serial attempt.
    
    out = []
    self_id = local_device_id()
    for d in devices:
        st, sn = store.latest(conn, d.id)
        out.append(device_view(d, st, sn, detail, self_id=self_id))
    conn.close()
    return out


@app.command("ls")
def cmd_ls(names: list[str] = typer.Argument(None, help="only these devices"),
           json_out: bool = typer.Option(False, "--json"),
           refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe"),
           online: bool = typer.Option(False, "--online", help="only reachable devices")):
    """List every device with live resource availability.

    [dim]Example:[/dim]  fleet ls --json
    """
    rows = _rows(list(names) if names else None, refresh=refresh)
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
    # Right-justifying a multi-line cell pads its short lines from the left and comes
    # out ragged, so these two flip left only when some device really has more than one
    # card. A fleet of single-GPU boxes renders exactly as it always did.
    tall = any(device_lines(r) > 1 for r in rows)
    for col, kw in (("", {}), ("NAME", {"no_wrap": True}), ("KIND", {"no_wrap": True}),
                    ("GPU", {"no_wrap": True, "overflow": "ellipsis",
                             "max_width": 24}),
                    ("VRAM FREE", {"justify": "left" if tall else "right",
                                   "no_wrap": True}),
                    ("CPU", {"justify": "right", "no_wrap": True}),
                    ("RAM FREE", {"justify": "right", "no_wrap": True}),
                    ("DISK FREE", {"justify": "left" if tall else "right",
                                   "no_wrap": True}),
                    ("$/HR", {"justify": "right", "no_wrap": True}),
                    ("AGE", {"justify": "right", "no_wrap": True}),
                    ("NOTE", {"no_wrap": True, "overflow": "ellipsis", "max_width": 42})):
        t.add_column(col, **kw)
    for r in rows:
        gpu, vram = gpu_cells_compact(r)
        note = r.get("error", {}).get("detail", "") if r["status"] != "ok" else (
            r["alerts"][0] if r["alerts"] else "")
        age = f"{r['telemetry_age_s']}s" if r["telemetry_age_s"] is not None else "-"
        t.add_row(_DOT.get(r["status"], "?"), name_cell(r),
                  r["kind"], gpu, vram,
                  str(r["cpu_cores"] or "-"),
                  f"{r['ram_free_gb']:.0f}G" if r["ram_free_gb"] else "-",
                  disk_cell(r),
                  f"${r['usd_per_hour']:.2f}" if r["usd_per_hour"] else "-",
                  age, note)
    console.print(t)
    from . import access as acl
    if note := acl.staleness_note():
        console.print(f"[yellow]![/yellow] [dim]{note}[/dim]")
    s = view["summary"]
    console.print(f"\n[dim]{s['online']}/{s['total']} online · {s['gpus_free']} free GPU(s)"
                  + (f" · ${s['hourly_burn']:.2f}/hr burning" if s["hourly_burn"] else "") + "[/dim]")


@app.command("show")
def cmd_show(name: str = typer.Argument(None, help="defaults to this machine"),
             json_out: bool = typer.Option(False, "--json"),
             refresh: bool = typer.Option(True, "--refresh/--no-refresh")):
    """Full detail for one device.

    [dim]Example:[/dim]  fleet show lin-xps
    """
    if name is None:
        name = _this_machine(inv.load(), "show").name
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
        console.print(f"  disk  {d['mount']:<12} {d['free_gb']:.0f} GB free"
                      f"  [dim]{d['use_pct']}% used[/dim]")
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
def cmd_add(ssh_command: str = typer.Argument(None, help='e.g. "ssh -p 58418 root@1.2.3.4"'),
            this_machine: bool = typer.Option(False, "--self",
                                              help="record the machine you are on, with no ssh"),
            name: str = typer.Option(None, "--name"),
            kind: str = typer.Option(None, "--kind", help="permanent|rental|shared|appliance|mobile"),
            json_out: bool = typer.Option(False, "--json"),
            no_key_prompt: bool = typer.Option(False, "--no-key-prompt",
                                               help="never offer to install a key"),
            dry_run: bool = typer.Option(False, "--dry-run")):
    """Add a device from a pasted ssh command.

    [dim]Example:[/dim]  fleet add "ssh -p 58418 root@1.2.3.4"
    """
    if this_machine == bool(ssh_command):
        err.print("[red]Give an ssh command, or --self -- not both, not neither.[/red]")
        raise typer.Exit(2)
    devices = inv.load()
    if this_machine:
        # No ssh at all: `fleet add "ssh localhost"` would need inbound sshd on a laptop,
        # which is the thing run_probe_local exists to avoid, and the center is never an
        # ssh target by design. It still has to be in its own inventory.
        dev, res = onboard_self(name=name, kind=kind,
                                taken_names={d.name for d in devices})
    else:
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
    if action == "restored":
        console.print(f"[green]✓[/green] restored [bold]{dev.name}[/bold] — it had been "
                      "removed, and everything recorded about it is back.")
    elif action == "endpoint_added":
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
        # "host is up but rejected our key" is the one failure a password can fix.
        if res.status is Status.AUTH_FAILED and not no_key_prompt:
            if not sys.stdin.isatty():
                console.print(f"  [dim]run [bold]fleet center --enroll {dev.name}[/bold] from a "
                              "terminal to install your key.[/dim]")
            elif typer.confirm(f"  Install your public key on {dev.name} now?", default=True):
                if _install_key(dev):
                    inv.save(devices)


def _install_key(dev, *, quiet: bool = False) -> bool:
    """Prompt once for a password and use it only to install a public key.

    The password is never stored, never logged and never passed as an argument. It buys
    exactly one thing -- key auth -- after which every other path in fleet works as it
    already does.
    """
    eps = inv.endpoints_of(dev)
    if not eps:
        err.print(f"[red]{dev.name} has no endpoint recorded[/red]")
        return False
    # The fleet key, never one from ~/.ssh. This key is fleet's handle on the machine:
    # it can be revoked fleet-wide without touching the key you push to GitHub with, and
    # the entry it leaves in authorized_keys says where it came from.
    try:
        path, pubkey = ensure_keypair()
    except KeyError as exc:
        err.print(f"[red]{exc}[/red]")
        return False
    if not sys.stdin.isatty():
        # Hanging on a prompt would be bad; capturing the password into whatever called
        # us would be worse. Refuse, and say exactly what to run instead.
        msg = ("  [dim]no terminal here — run [bold]fleet center --enroll "
               f"{dev.name}[/bold] yourself to install your key.[/dim]")
        (console if quiet else err).print(msg)
        return False

    ep = sorted(eps, key=lambda e: e.preference)[0]
    console.print(f"[dim]installing {path} on {ep.user}@{ep.target}[/dim]")
    password = getpass.getpass(f"Password for {ep.user}@{ep.target}: ")
    try:
        ok, output = install_key(ep, password, pubkey)
    finally:
        password = ""                      # not security, just hygiene: drop it promptly
    if ok:
        # Re-probe rather than recording a verdict: auth is derived from the last probe
        # now, so without this the device keeps reporting needs_key until someone runs
        # `fleet refresh`. It also proves the key actually works -- an append that
        # succeeds is not the same as a key sshd will accept.
        conn = store.connect()
        try:
            store.record(conn, dev.id, run_probe(ep, mode=dev.probe_mode,
                                                 disk_paths=dev.disk_paths))
        finally:
            conn.close()
        console.print(f"[green]✓[/green] key installed on {dev.name}; password discarded.")
    else:
        err.print(f"[red]Could not install the key.[/red]\n{output.strip()[-400:]}")
    return ok


@app.command("edit")
def cmd_edit(name: str = typer.Argument(None, help="defaults to this machine"),
             ssh_command: str = typer.Option(None, "--ssh", metavar="CMD",
                                             help='new address, e.g. "ssh -p 2222 root@5.6.7.8"'),
             disk_path: list[str] = typer.Option(None, "--disk-path", metavar="PATH",
                                                 help="watch this mount or directory for free "
                                                      "space; repeatable"),
             clear_disk_paths: bool = typer.Option(False, "--clear-disk-paths",
                                                   help="go back to autodetecting mounts"),
             new_name: str = typer.Option(None, "--name", metavar="NEW",
                                          help="rename it; the id and its history stay"),
             role: str = typer.Option(None, "--role", help="none | center | backup"),
             json_out: bool = typer.Option(False, "--json")):
    """Change a device's address or settings after it was added.

    Rentals recycle IPs and ports, so `--ssh` re-points a device without losing its
    name, tags, cost or history.

    [dim]Example:[/dim]  fleet edit blackwell --ssh "ssh -p 40001 root@5.6.7.8"
    """
    devices = inv.load()
    dev = _this_machine(devices, "edit") if name is None else inv.find(devices, name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)

    endpoint = resolve_command(ssh_command) if ssh_command else None
    paths = [] if clear_disk_paths else (list(disk_path) if disk_path else None)
    result = apply_edits(dev, name=new_name,
                         taken={d.name for d in inv.live(devices) if d is not dev},
                         endpoint=endpoint, disk_paths=paths,
                         role=None if role == "center" else role)
    if role == "center":
        # Flipping the role alone strands the fleet: spokes verify the list against the
        # key they have pinned, so a center nobody installed keys for and nobody signed
        # a handover from is a center no machine will accept.
        err.print("[red]Use [bold]fleet center NAME[/bold] to move the role.[/red]")
        err.print("  [dim]it installs the successor's key everywhere and verifies it "
                  "first; this flag only changed a label[/dim]")
        raise typer.Exit(2)

    if not result.changes:
        if _emit({"name": dev.name, "changes": []}, json_out):
            return
        console.print(f"[dim]· nothing to change on {dev.name}.[/dim]")
        return

    inv.save(devices)
    if result.previous_id:
        # the cache is keyed by id; without this the device looks brand new
        conn = store.connect()
        store.rename_device(conn, result.previous_id, dev.id)
        conn.close()

    if _emit({"name": dev.name, "id": dev.id, "changes": result.changes}, json_out):
        return
    console.print(f"[green]✓[/green] {dev.name}")
    for line in result.changes:
        console.print(f"  {line}")
    if endpoint is not None:
        console.print(f"  [dim]run `fleet refresh {dev.name}` to confirm it answers.[/dim]")


def configured_repo() -> str:
    """Where a device should clone fleet from.

    Falls back to this checkout's own origin, so the common case -- installing from the
    repo you are standing in -- needs no configuration at all.
    """
    configured = load_config().get("repo")
    if configured:
        return str(configured)
    try:
        root = Path(__file__).resolve().parent.parent.parent
        out = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def run_installer(ep, script: str, *, forward_agent: bool = True) -> tuple[int, str]:
    argv = build_install_argv(ep, forward_agent=forward_agent)
    p = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=900)
    return p.returncode, (p.stdout + p.stderr)


@app.command("install")
def cmd_install(name: str = typer.Argument(None,
                                          help="defaults to this machine"),
                repo: str = typer.Option(None, "--repo", metavar="URL",
                                         help="git URL to clone; defaults to config or this checkout"),
                ref: str = typer.Option("main", "--ref", help="branch or tag to install"),
                role: str = typer.Option(None, "--role",
                                         help="none (the only role a device takes here; "
                                              "move the center with `fleet center`)"),
                forward_agent: bool = typer.Option(True, "--forward-agent/--no-forward-agent",
                                                   help="authenticate the clone as you, "
                                                        "leaving no credential on the device")):
    """Install fleet on a device so it can hold a copy of your state.

    Every other device needs nothing installed. This is the exception: a backup node has
    to run fleet, so fleet has to be there. Re-running updates an existing install.

    [dim]Example:[/dim]  fleet install oracle
    """
    if role == "center":
        # Checked before anything reaches the network. It used to be validated after the
        # install had already run, so an invalid flag cost a full ssh timeout before
        # being told it was invalid.
        err.print("[red]Use [bold]fleet center NAME[/bold] to move the role.[/red]")
        raise typer.Exit(2)
    devices = inv.load()
    dev = _this_machine(devices, "update") if name is None else inv.find(devices, name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)
    eps = inv.endpoints_of(dev)
    if not eps:
        err.print(f"[red]{dev.name} has no endpoint recorded[/red]")
        raise typer.Exit(1)

    url = repo or configured_repo()
    if not url:
        err.print("[red]No repo to install from.[/red]  Pass [bold]--repo "
                  "git@github.com:you/fleet.git[/bold], or set [bold]repo:[/bold] in "
                  f"{INVENTORY_PATH.parent / 'config.yaml'}")
        raise typer.Exit(2)

    console.print(f"[dim]installing fleet on {dev.name} from {url} ({ref})[/dim]")
    code, output = run_installer(sorted(eps, key=lambda e: e.preference)[0],
                                 install_script(url, ref=ref),
                                 forward_agent=forward_agent)
    if code != 0:
        err.print(f"[red]Install failed[/red] (exit {code})\n{output.strip()[-600:]}")
        if code == 90:
            err.print("  [dim]the device could not fetch uv -- it may have no outbound "
                      "internet.[/dim]")
        # deliberately NOT recording the role: `fleet ls` claiming a backup that does not
        # exist is worse than no backup, because you would rely on it when the centre dies.
        raise typer.Exit(2)

    # This command doubles as the updater, so it must not change a role nobody asked
    # it to change: silently demoting the center on every update leaves `fleet sync`
    # with nowhere to go.
    # Only an explicit --role center is refused. `fleet install` doubles as `fleet
    # update`, so reinstalling on the existing center must leave it alone rather than
    # tripping over its own role.
    if role is None:
        role = dev.role
    if role != dev.role:
        dev.role = role
        inv.touch(dev)
    inv.save(devices)
    console.print(f"[green]✓[/green] {dev.name} is now [bold]{role}[/bold] — "
                  f"{output.strip().splitlines()[-1] if output.strip() else 'installed'}")


@lru_cache(maxsize=1)
def local_device_id() -> str:
    """This machine's identity, in the same shape onboard.py stamps on a probed device.

    Used only to notice that we ARE the center, so `fleet sync` can be safe to run
    everywhere rather than being a command you must remember not to run in one place.
    """
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(candidate).read_text().strip()
        except OSError:
            continue
        if value:
            return f"linux:machine-id:{value}"
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                             capture_output=True, text=True, timeout=5)
        found = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out.stdout)
        if found:
            return f"darwin:hwuuid:{found.group(1)}"
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def run_sync(ep, payload: str) -> tuple[int, str]:
    """Hand our inventory to the center and take back the merged result."""
    # a non-interactive shell may not have ~/.local/bin on PATH, which is exactly where
    # `fleet install` puts fleet.
    remote = 'sh -lc \'PATH="$HOME/.local/bin:$PATH" fleet sync --serve\''
    argv = build_argv(ep, remote=remote)
    # Sealed, because the far side runs this filter for anyone holding a key on it.
    from . import access as acl
    p = subprocess.run(argv, input=acl.seal(payload, telemetry=_telemetry_to_relay()),
                       capture_output=True, text=True, timeout=180)
    return p.returncode, (p.stdout if p.returncode == 0 else p.stdout + p.stderr)


def _show_version(value: bool):
    if value:
        from .setup import package_version
        console.print(f"fleet {package_version()}")
        raise typer.Exit()


@app.callback()
def _before_any_command(
    ctx: typer.Context,
    version: bool = typer.Option(None, "--version", callback=_show_version,
                                 is_eager=True, help="show the installed version"),
):
    """Only `--version` lives here now.

    This used to fire a detached background sync for nearly every command. That was
    reasonable when sync only merged inventories, and is not once sync also installs and
    removes keys: `fleet ls` would have quietly mutated credentials across the fleet
    every few minutes, unsupervised, with every exception swallowed by design. Sync is
    explicit now.
    """


@app.command("update")
def cmd_update(name: str = typer.Argument(None, help="defaults to this machine"),
               everywhere: bool = typer.Option(False, "--all",
                                               help="every device that can be reached"),
               repo: str = typer.Option(None, "--repo", metavar="URL"),
               ref: str = typer.Option("main", "--ref", help="branch or tag")):
    """Deploy the newest fleet from git.

    `fleet install` already re-runs as an update, but only one device at a time and only
    over ssh. This adds the two things you actually reach for: updating everything at
    once, and updating the machine you are standing on without connecting to it.

    [dim]Example:[/dim]  fleet update --all
    """
    devices = inv.load()
    url = repo or configured_repo()
    if not url:
        err.print("[red]No repo to update from.[/red]  Pass [bold]--repo "
                  "git@github.com:you/fleet.git[/bold], or set [bold]repo:[/bold] in "
                  f"{INVENTORY_PATH.parent / 'config.yaml'}")
        raise typer.Exit(2)

    if everywhere:
        targets = [d for d in inv.live(devices) if inv.endpoints_of(d)]
    elif name:
        one = inv.find(devices, name)
        if one is None:
            err.print(f"[red]No device named {name!r}[/red]")
            raise typer.Exit(1)
        targets = [one]
    else:
        targets = []

    me = local_device_id()
    script = install_script(url, ref=ref)
    ok = failed = 0

    # This machine first and without ssh. The center is never an ssh target, so
    # connecting to ourselves would fail on exactly the machine most likely to be
    # running the command.
    if not name or (targets and any(d.id == me for d in targets)):
        console.print(f"[dim]updating this machine from {url} ({ref})[/dim]")
        p = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        if p.returncode == 0:
            console.print("[green]✓[/green] this machine")
            ok += 1
        else:
            err.print(f"[red]✗[/red] this machine\n{(p.stderr or p.stdout)[-400:]}")
            failed += 1
        targets = [d for d in targets if d.id != me]

    for dev in targets:
        eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
        console.print(f"[dim]updating {dev.name}[/dim]")
        code, output = run_installer(eps[0], script)
        if code == 0:
            console.print(f"[green]✓[/green] {dev.name}")
            ok += 1
        else:
            # One unreachable device must not stop the rest: a fleet half-updated on
            # purpose is better than a fleet half-updated by an exception.
            err.print(f"[red]✗[/red] {dev.name} (exit {code}) "
                      f"[dim]{output.strip()[-120:]}[/dim]")
            failed += 1

    if ok + failed > 1 or failed:
        console.print(f"\n[dim]{ok} updated, {failed} failed[/dim]")
    if failed:
        raise typer.Exit(1)


@app.command("sync")
def cmd_sync(serve: bool = typer.Option(False, "--serve",
                                        help="run on the center: merge stdin, print the result"),
             json_out: bool = typer.Option(False, "--json")):
    """Merge this machine's inventory with the center's.

    Safe to run anywhere and repeatedly: the merge is a union, the newer record wins,
    and a device known to only one side is never dropped.

    [dim]Example:[/dim]  fleet sync
    """
    if serve:
        from . import access as acl

        raw = sys.stdin.read()
        # The inventory carries the endpoints that decide where `fleet ssh` dials, and
        # this filter runs on a spoke that every granted peer holds a key for. Verifying
        # the access list and taking the routing on trust would have secured the policy
        # and left the routes open, so the whole envelope is checked.
        pinned = acl.trusted_center_pubkey()
        relayed = []
        try:
            if pinned:
                body, relayed = acl.unseal_with_telemetry(raw, pinned)
            else:
                body = acl.unseal_first_contact(raw)
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        acl.note_center_seen()
        if relayed:
            _record_relayed(relayed)
        try:
            incoming = inv.loads(body)
        except Exception as exc:
            # A truncated pipe must never be read as "the other side has no devices".
            err.print(f"[red]unreadable inventory on stdin:[/red] {exc}")
            raise typer.Exit(2)
        # merged against whatever the file holds *now*, under the lock: another
        # command on this machine may have committed while we were reading stdin.
        merged, changes = inv.update(lambda current: inv.merge(current, incoming))
        sys.stdout.write(inv.dumps(merged))
        return

    devices = inv.load()
    center = next((d for d in inv.live(devices) if d.role == "center"), None)
    if center is None:
        err.print("[red]No center designated.[/red]  Pick one:  "
                  "[bold]fleet edit NAME --role center[/bold]")
        raise typer.Exit(2)
    if center.id and center.id == local_device_id():
        _sweep(devices)
        return
    eps = inv.endpoints_of(center)
    if not eps:
        err.print(f"[red]{center.name} is the center but has no endpoint recorded[/red]")
        raise typer.Exit(2)

    code, output = run_sync(sorted(eps, key=lambda e: e.preference)[0], inv.dumps(devices))
    if code != 0:
        # sync is not on the critical path: every command still works from local state.
        err.print(f"[red]sync failed[/red] (exit {code})\n{output.strip()[-400:]}")
        raise typer.Exit(2)
    try:
        returned = inv.loads(output)
    except Exception as exc:
        err.print(f"[red]the center returned something unreadable:[/red] {exc}")
        raise typer.Exit(2)

    # NOT merged against `devices`: that list was loaded before the round trip, and
    # saving it back would erase anything committed while we were waiting.
    merged, changes = inv.update(
        lambda current: inv.merge(current, returned, authoritative=True))
    if _emit({"center": center.name, "devices": len(merged), "changes": changes}, json_out):
        return
    console.print(f"[green]✓[/green] synced with [bold]{center.name}[/bold] "
                  f"({len(merged)} devices)")
    for line in changes:
        console.print(f"  {line}")
    if not changes:
        console.print("  [dim]already up to date.[/dim]")


def _live_tick(conn, devices, schedule: Schedule, cfg, detail: Detail) -> list[dict]:
    """Probe whatever is due, then render every device from the cache.

    The probe is wrapped because one unreachable host must not end the session:
    probe_many already isolates failures inside the sweep, and the loop around it has
    to do the same or a dropped network takes the whole view down.
    """
    now = time.monotonic()
    me = local_device_id()
    due = [d for d in schedule.due(devices, now) if d.probeable]
    if due:
        groups: dict[tuple[str, tuple[str, ...]], dict[str, list]] = {}
        for d in due:
            if me and d.id == me:
                res = run_probe_local(mode=d.probe_mode, disk_paths=d.disk_paths)
                store.record(conn, d.id, res)
                schedule.record(d.id, ok=res.ok, now=now)
                continue
            groups.setdefault((d.probe_mode, tuple(d.disk_paths)), {})[d.id] = \
                inv.endpoints_of(d)
        for (mode, paths), subset in groups.items():
            try:
                results = probe_many(subset, mode=mode, disk_paths=list(paths),
                                     timeout=float(cfg.probe_timeout_s),
                                     connect_timeout=int(cfg.connect_timeout_s),
                                     max_workers=int(cfg.max_workers))
            except Exception:
                for dev_id in subset:
                    schedule.record(dev_id, ok=False, now=now)
                continue
            for dev_id, res in results.items():
                store.record(conn, dev_id, res)
                schedule.record(dev_id, ok=res.ok, now=now)

    rows = []
    for d in devices:
        st, sn = store.latest(conn, d.id)
        rows.append(device_view(d, st, sn, detail, self_id=me))
    return rows


def _endpoint_for(dev, user: str):
    """The device's best route, dialled as the user this edge is about.

    The edge names whose authorized_keys we are editing, which is not always the user the
    endpoint happens to record -- a box answers as both root@ and ubuntu@, and writing
    the wrong one's file is a grant that appears to work and never does.
    """
    eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
    if not eps:
        return None
    ep = eps[0]
    return replace(ep, user=user or ep.user)


def _telemetry_to_relay() -> list[dict]:
    """What we measured ourselves, for machines the far side may not be able to reach.

    First-hand only: relaying a row that was itself relayed would let a reading drift
    between machines with nothing to say how far it had travelled or how old it really
    was.
    """
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT device_id, status, last_probe_at FROM device_state "
            "WHERE source='self'").fetchall()
        out = []
        for r in rows:
            _, snap = store.latest(conn, r["device_id"])
            out.append({"device_id": r["device_id"], "status": r["status"],
                        "probed_at": r["last_probe_at"], "snapshot": snap})
        return out
    finally:
        conn.close()


def _record_relayed(rows: list) -> None:
    """Store rows the center measured, marked as second-hand.

    Never overwrites a probe we ran: `store.latest` prefers first-hand, so our own
    reading of a machine we can reach always wins over the center's view of it.
    """
    from .models import ProbeResult, Snapshot, Status

    by = ""
    conn = store.connect()
    try:
        for row in rows:
            if not isinstance(row, dict) or not row.get("device_id"):
                continue
            snap = None
            if isinstance(row.get("snapshot"), dict):
                with suppress(Exception):
                    snap = Snapshot(**{k: v for k, v in row["snapshot"].items()
                                       if k in Snapshot.__dataclass_fields__})
            with suppress(ValueError):
                store.record(conn, row["device_id"],
                             ProbeResult(status=Status(row.get("status") or "unknown"),
                                         snapshot=snap),
                             source="broadcast", probed_by=by or "center")
    finally:
        conn.close()


def _sweep(devices) -> None:
    """The center's pass over the fleet: make authorized_keys match the access list.

    Only the center reaches here, and only when `fleet sync` is run deliberately -- this
    installs and removes credentials on every machine, which is not something to do from
    a background timer nobody is watching.
    """
    from . import access as acl
    from . import reconcile as rec

    try:
        acc = acl.load()
    except acl.AccessError as exc:
        console.print(f"[dim]· {exc}[/dim]")
        return

    ledger = rec.plan(acc, rec.load_ledger())
    if why := rec.refuses_to_run(acc, ledger):
        err.print(f"[red]{why}[/red]")
        raise typer.Exit(2)

    by_id = {d.id: d for d in inv.live(devices)}
    pending = [(k, st) for k, st in ledger.items() if not st.converged]
    if not pending:
        console.print("[dim]· access is up to date[/dim]")
        return

    conn = store.connect()
    done = failed = 0
    try:
        for key, st in pending:
            src, dst, user = key.split(">")
            # the ledger's copy first: a revoke usually runs *because* the machine was
            # dropped from the list, so the pin is often already gone
            dev = by_id.get(st.dst_device or (acc.keys.get(dst) or {}).get("device_id", ""))
            install = st.desired == "present"
            st.attempts += 1
            st.last_attempt_at = int(time.time())
            if dev is None:
                st.last_error = "no device record for this machine"
                failed += 1
                continue
            ep = _endpoint_for(dev, user)
            if ep is None:
                st.last_error = "no endpoint recorded"
                failed += 1
                continue
            ok, out = rec.apply_edge(acc, (src, dst, user), ep, install=install,
                                     windows=(dev.ssh_auth == "windows"))
            if ok:
                st.observed, st.last_error = st.desired, ""
                done += 1
                verb = "installed on" if install else "removed from"
                console.print(f"[green]✓[/green] {acc.name_of(src)}'s key {verb} {dev.name}")
                # While we are connected anyway: a machine that cannot reach this one
                # will otherwise have no telemetry for it at all.
                with suppress(Exception):
                    store.record(conn, dev.id,
                                 run_probe(ep, mode=dev.probe_mode,
                                           disk_paths=dev.disk_paths))
            else:
                st.last_error = out
                failed += 1
                # Not an error: a device that is off is an edge that has not converged.
                console.print(f"[yellow]·[/yellow] {dev.name} not reached "
                              f"[dim]({out[:60]})[/dim]")
    finally:
        conn.close()
        rec.save_ledger(ledger)

    console.print(f"\n[dim]{done} applied, {failed} still pending[/dim]"
                  + ("  [dim]-- `fleet access` shows what is outstanding[/dim]"
                     if failed else ""))


@app.command("top")
def cmd_top(name: str = typer.Argument(None, help="one device, instead of the whole fleet"),
            interval: float = typer.Option(2.0, "--interval", "-i",
                                           help="seconds between refreshes")):
    """Live view of the fleet, or of one device. Like htop, for your machines.

    Shared hosts keep their own slow cadence (shared_min_interval_s) and are shown as
    ageing rather than live, and anything unreachable backs off instead of being
    redialled every couple of seconds.

    [dim]Example:[/dim]  fleet top lin-xps -i 1
    """
    cfg = load_config()
    devices = inv.live(inv.load())
    if name:
        one = inv.find(devices, name)
        if one is None:
            err.print(f"[red]No device named {name!r}[/red]")
            raise typer.Exit(1)
        devices = [one]

    schedule = Schedule(interval=interval,
                        shared_interval=float(cfg.shared_min_interval_s))
    conn = store.connect()
    detail = Detail.FULL
    live_within = max(int(interval * 3), 5)

    def frame():
        rows = _live_tick(conn, devices, schedule, cfg, detail)
        if name:
            return render_device(rows[0], live_within)
        view = fleet_view(rows)
        table = render_fleet(rows, view["summary"], live_within)
        s = view["summary"]
        return Group(table, Text.from_markup(
            f"\n[dim]{s['online']}/{s['total']} online · {s['gpus_free']} free GPU(s)"
            + (f" · ${s['hourly_burn']:.2f}/hr" if s["hourly_burn"] else "")
            + "  ·  q quit   r refresh[/dim]"))

    # A live loop in a pipe would spin forever, and an agent is exactly what would run
    # it that way. One frame is also the more useful thing for a script.
    if not sys.stdout.isatty():
        console.print(frame())
        conn.close()
        return

    from rich.live import Live
    try:
        with _raw_stdin(), Live(frame(), console=console, screen=True,
                                refresh_per_second=8) as live:
            while True:
                key = _key_pressed(interval)
                if key in ("q", "Q", "\x03", "\x04"):
                    break
                if key in ("r", "R"):
                    schedule = Schedule(interval=interval,      # everything due at once
                                        shared_interval=float(cfg.shared_min_interval_s))
                live.update(frame())
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


@contextmanager
def _raw_stdin():
    """cbreak mode so single keys arrive without Enter. Restored no matter how we
    leave, or the user's shell is left unusable."""
    if not sys.stdin.isatty():
        yield
        return
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _key_pressed(timeout: float) -> str | None:
    """Doubles as the frame delay: waits for a key, or returns when the interval is up."""
    import select as _select
    if not sys.stdin.isatty():
        return None
    if _select.select([sys.stdin], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return None


@app.command("rm")
def cmd_rm(name: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Remove a device from the inventory.

    [dim]Example:[/dim]  fleet rm blackwell
    """
    devices = inv.load()
    dev = inv.find(devices, name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)
    from . import access as acl

    # Removing a machine revokes its keys everywhere, which only the center can do.
    # Removing *yourself* is a different act -- leaving -- and needs nobody's permission,
    # because you own the machine you are standing on.
    itself = bool(dev.id) and dev.id == local_device_id()
    if not itself:
        try:
            if not acl.is_center(acl.load()):
                err.print(f"[red]Only the center can remove {dev.name}.[/red]")
                err.print("  [dim]a machine can remove itself -- that is leaving -- but "
                          "removing another revokes its keys, which only the center "
                          "can do[/dim]")
                raise typer.Exit(2)
        except acl.AccessError:
            pass          # no fleet yet: the inventory is just a list, remove freely

    if not yes and not typer.confirm(f"Remove {dev.name} ({dev.kind.value})?"):
        raise typer.Exit(1)

    # Drop its edges before the record, so the sweep still knows where to go: the ledger
    # kept the device on each edge precisely so a revoke survives losing the pin.
    revoked = 0
    try:
        acc = acl.load()
        if acl.is_center(acc):
            fps = [fp for fp, m in acc.keys.items() if m.get("device_id") == dev.id]
            for fp in fps:
                acc.allow = [e for e in acc.allow if fp not in (e.src, e.dst)]
                acc.keys.pop(fp, None)
                revoked += 1
            if fps:
                acl.save(acc)
    except acl.AccessError:
        pass

    inv.remove(devices, dev)
    inv.save(devices)
    console.print(f"[green]✓[/green] removed {dev.name}")
    if revoked:
        console.print("  [yellow]its keys are still installed[/yellow] until the next "
                      "[bold]fleet sync[/bold] reaches each machine.")


@app.command("probe", hidden=True)
def cmd_probe(name: str = typer.Argument(None, help="defaults to this machine"),
              raw: bool = typer.Option(False, "--raw", help="print payload stdout")):
    """Probe one device directly. --raw captures a new parser test fixture.

    Hidden: its real job is capturing fixtures for the parser tests, and without --raw it
    says what `fleet show --json` already says.

    [dim]Example:[/dim]  fleet probe lin-xps --raw
    """
    if name is None:
        # No ssh at all for our own machine: requiring sshd, a key and a network path to
        # inspect the box we are running on is a lot of parts for no extra information.
        res = run_probe_local()
        console.print_json(jsonlib.dumps(
            {"status": res.status.value, "latency_ms": res.latency_ms,
             "snapshot": res.snapshot.to_dict() if res.snapshot else None}, default=str))
        raise typer.Exit(0 if res.ok else 1)
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
                          remote="sh -s", env=probe_env(dev.probe_mode, dev.disk_paths))
        p = subprocess.run(argv, input=PAYLOAD.read_text(), capture_output=True, text=True)
        sys.stdout.write(p.stdout)
        raise typer.Exit(0 if p.returncode == 0 else 1)
    res = run_probe(sorted(eps, key=lambda e: e.preference)[0], mode=dev.probe_mode,
                    disk_paths=dev.disk_paths)
    console.print_json(jsonlib.dumps(
        {"status": res.status.value, "latency_ms": res.latency_ms,
         "error": res.error_detail, "snapshot": res.snapshot.to_dict() if res.snapshot else None},
        default=str))


@app.command("ssh", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def cmd_ssh(ctx: typer.Context, name: str):
    """Open a shell on a device, or run a command: `fleet ssh lin-xps -- nvidia-smi`.

    This exists so credentials never have to reach an agent: the wrapper resolves the
    endpoint and connects, rather than handing out a connection string plus a password.

    [dim]Example:[/dim]  fleet ssh lin-xps -- nvidia-smi
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
    conn = store.connect()
    try:
        cached, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    # Read from the last probe rather than a field on the device: a stored OS would ride
    # inventory.merge, where a peer with a fast clock could flip a host's platform and
    # change which shell we hand it a command in.
    windows = str((snap or {}).get("os", "")).lower().startswith(("microsoft windows",
                                                                 "windows"))
    if auth_of(dev, cached) == "needs_key":
        err.print(f"[yellow]{dev.name} rejected our key.[/yellow] Install one:")
        err.print(f"  [bold]fleet center --enroll {dev.name}[/bold]")
        raise typer.Exit(2)
    argv = ["ssh"]
    # The fleet key, or `fleet ssh` connects with a personal key that fleet no longer
    # installs anywhere -- and this is the most-used command in the tool.
    if FLEET_KEY.exists():
        argv += ["-i", str(FLEET_KEY)]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.identity:
        argv += ["-i", ep.identity]
    if ep.jump:
        argv += ["-J", ep.jump]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    extra = [a for a in ctx.args if a != "--"]
    if extra:
        argv.append(remote_command(extra, windows=windows))

    os.execvp("ssh", argv)      # replace this process; ssh owns the tty from here


@app.command("access")
def cmd_access(target: str = typer.Argument(None, help="one machine, instead of all"),
               allow: str = typer.Option(None, "--allow", metavar="MACHINE",
                                         help="let MACHINE reach the target"),
               deny: str = typer.Option(None, "--deny", metavar="MACHINE",
                                        help="stop MACHINE reaching the target"),
               user: str = typer.Option("root", "--user", help="whose authorized_keys"),
               migrate: bool = typer.Option(False, "--migrate",
                                            help="spend passwords an older fleet stored"),
               json_out: bool = typer.Option(False, "--json")):
    """Who may reach what, and change it.

    Granting installs a key; revoking removes one. Both are things the center does to a
    machine over ssh, so both can be pending -- and a revoke that has not reached its
    target is reported as not in effect, never as done.

    [dim]Example:[/dim]  fleet access oracle --allow lin-xps
    """
    from . import access as acl
    from . import reconcile as rec

    if migrate:
        _migrate_passwords()
        return

    try:
        current = acl.load()
    except acl.AccessError as exc:
        err.print(f"[red]{exc}[/red]")
        err.print("  [dim]start one with [bold]fleet center --init[/bold][/dim]")
        raise typer.Exit(2)

    centre = acl.is_center(current)
    if (allow or deny) and not centre:
        if deny:
            # Never queue a revoke. Deferring one silently looks identical to having
            # done it, which is the failure this whole design exists to remove.
            err.print("[red]Only the center can revoke access.[/red]")
            raise typer.Exit(2)
        _file_request(current, target, allow, user)
        console.print(f"[yellow]Not the center[/yellow] -- filed a request for "
                      f"{allow} -> {target}. It applies when the center next sweeps.")
        return

    if allow or deny:
        try:
            dst = acl.resolve(current, target)
            src = acl.resolve(current, allow or deny)
            changed = (acl.grant(current, src, dst, user=user) if allow
                       else acl.revoke(current, src, dst, user=user))
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        if changed:
            acl.save(current)
        verb = "granted" if allow else "revoked"
        console.print(f"[green]✓[/green] {verb} {current.name_of(src)} -> "
                      f"{current.name_of(dst)}"
                      + ("" if changed else "  [dim](already so)[/dim]"))
        console.print("  [dim]run [bold]fleet sync[/bold] to apply it[/dim]")

    ledger = rec.load_ledger()
    rows = []
    for edge in sorted(current.edges()):
        src, dst, who = edge
        if target and dst != acl.resolve(current, target):
            continue
        st = ledger.get(">".join(edge), rec.EdgeState())
        rows.append({"from": current.name_of(src), "to": current.name_of(dst),
                     "user": who, "state": st.observed,
                     "pending_s": (int(time.time()) - st.pending_since)
                                  if not st.converged and st.pending_since else 0,
                     "last_error": st.last_error})
    if _emit({"center": current.name_of(current.center), "edges": rows}, json_out):
        return
    if not rows:
        console.print("[dim]no access granted yet[/dim]")
        return
    t = Table(box=None, pad_edge=False, header_style="bold")
    for col in ("", "FROM", "TO", "USER", "STATE", "NOTE"):
        t.add_column(col, no_wrap=(col != "NOTE"))
    for r in rows:
        live = r["state"] == "present"
        dot = "[green]●[/green]" if live else "[yellow]○[/yellow]"
        note = r["last_error"] or ("" if live else "not applied yet")
        if r["pending_s"]:
            note = f"pending {r['pending_s'] // 60}m · {note}" if note else \
                   f"pending {r['pending_s'] // 60}m"
        t.add_row(dot, r["from"], r["to"], r["user"], r["state"], note)
    console.print(t)
    if not centre:
        console.print(f"\n[dim]center is {current.name_of(current.center)}; "
                      "changes are filed as requests from here[/dim]")


def _migrate_passwords() -> None:
    """Spend each stored password once, to install this machine's fleet key.

    Install, verify, then remove -- in that order, never the reverse. A password dropped
    before the key is proven leaves a host nobody can reach, and the whole point of the
    change is that there is no second copy of it anywhere.

    Reading them needs `pyrage`, which is now an optional extra. That is deliberate: the
    encrypted file is still on disk, and removing the only thing that can read it in the
    same release that added the migration would strand it.
    """
    from . import secrets as sec
    from .keys import ensure_keypair, install_key

    try:
        data = sec.read_secrets(sec.SECRETS_PATH, sec.load_identity())
    except Exception as exc:
        err.print(f"[red]Cannot read the old secrets:[/red] {exc}")
        # escaped: rich reads a bare [migrate] as a style tag and silently eats it,
        # leaving the user an install command that does not install the reader
        err.print("  [dim]install the reader with [bold]uv tool install "
                  r"'fleet-broker\[migrate]'[/bold][/dim]")
        raise typer.Exit(2)
    if not data:
        console.print("[dim]nothing stored -- nothing to migrate[/dim]")
        return

    _, pub = ensure_keypair()
    devices = inv.load()
    failed = []
    for name, password in sorted(data.items()):
        dev = inv.find(devices, name)
        eps = inv.endpoints_of(dev) if dev else []
        if not eps:
            failed.append((name, "no endpoint recorded"))
            continue
        ok, out = install_key(sorted(eps, key=lambda e: e.preference)[0], password, pub)
        if ok:
            console.print(f"[green]✓[/green] {name}")
        else:
            failed.append((name, out.strip()[-120:]))
    for name, why in failed:
        err.print(f"[red]✗[/red] {name}: {why}")
    if failed:
        err.print(f"\n[yellow]Keeping {sec.SECRETS_PATH.name}[/yellow] -- "
                  f"{len(failed)} of {len(data)} could not be migrated.")
        raise typer.Exit(1)
    # "removed", not "shredded": os.replace on a journalling filesystem or an SSD does
    # not reliably destroy the old blocks, and saying otherwise would be a lie that
    # outlives whoever wrote it.
    for path in (sec.SECRETS_PATH, sec.IDENTITY_PATH):
        with suppress(OSError):
            path.unlink()
    console.print(f"\n[green]✓[/green] all {len(data)} migrated; stored passwords removed.")


def _file_request(current, target: str, allow: str, user: str) -> None:
    """Ask the center for an edge we cannot create ourselves.

    Written to our own outbox and carried by the next sweep. A request is not a grant --
    the center decides -- but a request matching an edge that already exists is simply
    key placement that failed, and reconciles without anyone being asked.
    """
    from . import access as acl

    out = []
    if acl.OUTBOX_PATH.exists():
        out = (yaml.safe_load(acl.OUTBOX_PATH.read_text()) or {}).get("requests", [])
    entry = {"to": target, "from": allow, "user": user, "at": int(time.time())}
    if entry not in [{k: v for k, v in r.items() if k != "at"} | {"at": r.get("at")}
                     for r in out]:
        out.append(entry)
    acl.OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    acl.OUTBOX_PATH.write_text(yaml.safe_dump({"requests": out}, sort_keys=False))


@app.command("center")
def cmd_center(name: str = typer.Argument(None, help="hand the role to this machine"),
               init: bool = typer.Option(False, "--init",
                                         help="start a fleet with this machine as center"),
               pubkey: bool = typer.Option(False, "--pubkey",
                                           help="print the key to pre-place on a host"),
               export: bool = typer.Option(False, "--export",
                                           help="print the access list and pins"),
               enroll: str = typer.Option(None, "--enroll", metavar="MACHINE",
                                          help="put this fleet's key on a host for the "
                                               "first time, using a password typed once"),
               accept: bool = typer.Option(False, "--accept",
                                           help="take the role a handover offered"),
               leave: bool = typer.Option(False, "--leave",
                                          help="remove this fleet's keys from this machine"),
               json_out: bool = typer.Option(False, "--json"),
               force: bool = typer.Option(False, "--force")):
    """Who decides, and handing that over.

    Only the current center can name the next one. No machine may promote itself, so an
    unplanned loss of the center means re-configuring by hand -- which is the price of
    there being exactly one machine that can open every door.

    [dim]Example:[/dim]  fleet center lin-xps
    """
    from . import access as acl
    from .keys import ensure_keypair

    if pubkey:
        # Deliberately works with nothing reachable and no inventory: the moment you
        # want this is before the machine exists, writing a cloud-init file.
        _, pub = ensure_keypair()
        print(pub)
        return

    if enroll:
        # The bootstrap the sweep cannot do: the sweep writes with a key the host already
        # accepts, so a host the center has never reached needs one put there some other
        # way. Deliberately before the access list is loaded -- this is how a fleet gets
        # its first machine, and requiring the fleet to exist first would be circular.
        dev = inv.find(inv.load(), enroll)
        if dev is None:
            err.print(f"[red]No device named {enroll!r}[/red]")
            raise typer.Exit(1)
        if not _install_key(dev):
            raise typer.Exit(2)
        console.print(f"  [dim]now grant it something: [bold]fleet access {dev.name} "
                      f"--allow <machine>[/bold][/dim]")
        return

    if init:
        key_path, pub = ensure_keypair()
        dev, _ = onboard_self()
        try:
            acc = acl.bootstrap(dev.name, pub, dev.id)
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        console.print(f"[green]✓[/green] fleet {acc.fleet_id} started; "
                      f"{dev.name} is the center.")
        console.print(f"  [dim]key to pre-place on locked-down hosts: "
                      f"[bold]fleet center --pubkey[/bold][/dim]")
        return

    try:
        acc = acl.load()
    except acl.AccessError as exc:
        err.print(f"[red]{exc}[/red]")
        err.print("  [dim]start one with [bold]fleet center --init[/bold][/dim]")
        raise typer.Exit(2)

    if accept:
        _accept_handover(acc)
        return
    if leave:
        _leave_fleet(acc)
        return
    if export:
        print(acl.dumps(acc))
        return
    if name:
        _handover(acc, name, force=force)
        return

    # bare: status
    centre = acl.is_center(acc)
    if _emit({"center": acc.name_of(acc.center), "is_center": centre,
              "fleet_id": acc.fleet_id, "machines": len(acc.keys),
              "edges": len(acc.edges()),
              "last_seen_s": (int(time.time()) - acl.center_last_seen())
                             if acl.center_last_seen() else None}, json_out):
        return
    console.print(f"center   [bold]{acc.name_of(acc.center)}[/bold]"
                  + ("  [dim]← this machine[/dim]" if centre else ""))
    console.print(f"fleet    {acc.fleet_id}")
    console.print(f"machines {len(acc.keys)}   edges {len(acc.edges())}")
    if note := acl.staleness_note():
        console.print(f"[yellow]![/yellow] {note}")
    if not centre:
        console.print("\n[dim]Changes are made on the center. Losing it means "
                      "re-configuring by hand -- keep a copy: [bold]fleet center "
                      "--export[/bold][/dim]")


def _accept_handover(acc) -> None:
    """Phase two, on the successor: prove we can write, then take the role.

    The check is a no-op marker-block edit on every machine -- drop our own block and put
    it straight back. Probing would only prove our key is *present*; it says nothing
    about whether the file can be written, and on Windows every way that fails is
    silent, so a handover verified by probing would hand the fleet to a machine that
    cannot manage it and discover that only after the predecessor was gone.
    """
    from . import access as acl
    from . import reconcile as rec
    from .authkeys import posix_sync_command
    from .keys import ensure_keypair

    _, pub = ensure_keypair()
    mine = acl.fingerprint(pub)
    if mine == acc.center:
        console.print("[dim]already the center[/dim]")
        return
    if mine not in acc.keys:
        err.print("[red]This machine is not in the access list,[/red] so no handover "
                  "could have named it.")
        raise typer.Exit(2)

    devices = {d.id: d for d in inv.live(inv.load())}
    targets = [(fp, m) for fp, m in acc.keys.items() if fp != mine]
    unwritable = []
    for fp, meta in targets:
        dev = devices.get(meta.get("device_id", ""))
        eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference) if dev else []
        if not eps:
            unwritable.append((meta.get("name", fp[:18]), "no endpoint recorded"))
            continue
        # drop-then-append of our own block: idempotent, and it changes nothing if it
        # works, which is what makes it safe to run as a test
        script = posix_sync_command(acc.fleet_id, mine, user=eps[0].user, pubkey=pub)
        ok, out = rec._remote(eps[0], script, windows=False)
        name = meta.get("name", fp[:18])
        if ok:
            console.print(f"[green]✓[/green] can write {name}")
        else:
            unwritable.append((name, out[:80]))
            console.print(f"[red]✗[/red] {name} [dim]{out[:60]}[/dim]")

    if unwritable:
        err.print(f"\n[red]Not taking the role.[/red] {len(unwritable)} machine(s) "
                  "cannot be written from here, and a center that cannot write is a "
                  "fleet nobody can manage:")
        for name, why in unwritable:
            err.print(f"  {name}: {why}")
        err.print("\n  [dim]the current center still holds the role; fix these and "
                  "run this again[/dim]")
        raise typer.Exit(2)

    acc.center = mine
    acl.save(acc)
    console.print(f"\n[green]✓[/green] this machine is now the center of {acc.fleet_id}.")
    console.print("  [dim]run [bold]fleet sync[/bold] to sweep, then retire the old one "
                  "with [bold]fleet rm[/bold] on it if it is leaving[/dim]")


def _leave_fleet(acc) -> None:
    """Strip this fleet's keys from this machine. No permission required.

    You own your machines; the center does not get a veto. It cannot reliably tell
    'left' from 'down' either -- both look like an auth failure -- so this is a courtesy
    to the center as much as a right of the machine.
    """
    from .authkeys import posix_sync_command

    import subprocess
    for fp in acc.keys:
        script = posix_sync_command(acc.fleet_id, fp, pubkey=None)
        subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    console.print(f"[green]✓[/green] removed fleet {acc.fleet_id}'s keys from this machine.")
    console.print("  [dim]the center will see this as unreachable until you tell it[/dim]")


def _handover(acc, name: str, *, force: bool) -> None:
    """Give the role away. The one irreversible command in the tool."""
    from . import access as acl

    if not acl.is_center(acc):
        err.print("[red]Only the center can hand the role over.[/red]")
        err.print(f"  [dim]the center is {acc.name_of(acc.center)}[/dim]")
        raise typer.Exit(2)
    try:
        successor = acl.resolve(acc, name)
    except acl.AccessError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if successor == acc.center:
        console.print(f"[dim]{name} is already the center.[/dim]")
        return
    # Phase one. The successor's key goes everywhere and a signed record names it, but
    # nothing is retired yet: proving the successor can *write* each authorized_keys
    # requires the successor to try, and on Windows both ways that fails are silent. So
    # it finishes the job from its own side.
    fp = successor
    pub = (acc.keys.get(fp) or {}).get("pubkey", "")
    if not pub:
        err.print(f"[red]No pinned key for {name}.[/red]  Enrol it first:  "
                  f"[bold]fleet center --enroll {name}[/bold]")
        raise typer.Exit(2)

    added = 0
    for other in acc.keys:
        if other != fp and acl.grant(acc, fp, other):
            added += 1
    acl.save(acc)
    console.print(f"[green]✓[/green] {name} granted access to {added} machine(s)")

    record = acl.handover_record(acc, fp)
    signed = acl.sign(record)
    bundle = acl.CACHE_PATH.with_name("handover.yaml")
    bundle.write_text(yaml.safe_dump({"record": record, "signature": signed},
                                     sort_keys=False))

    console.print(f"\n[bold]Two things left, in this order.[/bold]")
    console.print(f"  1. [bold]fleet sync[/bold] here, to install {name}'s key everywhere")
    console.print(f"  2. on {name}: [bold]fleet center --accept[/bold]")
    console.print(f"\n[dim]It verifies it can actually write each authorized_keys before "
                  f"taking the role -- probing only proves a key is present. Nothing is "
                  f"retired until it succeeds, so this machine stays the center until "
                  f"then.[/dim]")
    console.print(f"[dim]handover record: {bundle}[/dim]")


@app.command("setup")
def cmd_setup(
    target: str = typer.Option("auto", "--target",
                               help="claude | codex | hermes | all | auto (whatever is installed)"),
    project: bool = typer.Option(False, "--project",
                                 help="write into the current directory, not your home"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would change; write nothing"),
    remove: bool = typer.Option(False, "--uninstall", help="remove what setup installed"),
):
    """Teach your coding agents to use fleet.

    Claude Code gets a skill, which costs nothing until a task actually needs a machine.
    Codex gets a marked region in AGENTS.md; anything else in that file is left alone.

    [dim]Example:[/dim]  fleet setup --dry-run
    """
    root = Path.cwd() if project else Path.home()
    if target == "auto":
        # cwd tells us nothing about which agents you use, so a project install
        # assumes the one whose layout is identical in both scopes.
        targets = ["claude"] if project else detect_targets(root)
    elif target == "all":
        targets = list(TARGETS)
    else:
        targets = [target]

    if unknown := [t for t in targets if t not in TARGETS]:
        err.print(f"[red]Unknown target {unknown[0]!r}[/red]  "
                  f"({' | '.join(TARGETS)} | all | auto)")
        raise typer.Exit(2)
    if not targets:
        err.print("[yellow]No coding agent found.[/yellow]  Looked for ~/.claude and "
                  "~/.codex.  Force one with [bold]--target claude[/bold].")
        raise typer.Exit(1)

    cmd = fleet_command()
    changes = (uninstall(root, targets, dry_run=dry_run, project=project) if remove
               else install(root, targets, cmd, dry_run=dry_run, project=project))

    for c in changes:
        colour = {"created": "green", "updated": "green",
                  "removed": "yellow"}.get(c.action, "dim")
        console.print(f"  [{colour}]{c.action:<9}[/{colour}] {c.path}")
    if dry_run:
        console.print("\n[dim]--dry-run: nothing was written.[/dim]")
    elif not remove and cmd != "fleet":
        err.print(f"\n[yellow]fleet is not on your PATH[/yellow], so the skill points at "
                  f"{cmd}.\n  Install it properly and re-run setup: "
                  "[bold]uv tool install --editable .[/bold]")


@app.command("paths")
def cmd_paths():
    """Show where fleet keeps its state.

    [dim]Example:[/dim]  fleet paths
    """
    from . import access as acl
    from .config import CONFIG_DIR, FLEET_KEY, STATE_DIR

    console.print(f"inventory  {INVENTORY_PATH}")
    console.print(f"fleet key  {FLEET_KEY}   [dim](never regenerate: it is this "
                  "machine's identity)[/dim]")
    console.print(f"access     {acl.ACCESS_PATH}   [dim](center only — the authority)[/dim]")
    console.print(f"ledger     {acl.LEDGER_PATH}   [dim](center only — what has landed)[/dim]")
    console.print(f"seen       {acl.CACHE_PATH}   [dim](the center's key, and when it "
                  "last swept)[/dim]")
    console.print(f"outbox     {acl.OUTBOX_PATH}   [dim](requests we have filed)[/dim]")
    console.print(f"cache      {DB_PATH}   [dim](disposable — delete and re-probe)[/dim]")
    if CONFIG_DIR == STATE_DIR:
        # Worth saying out loud: it is why every filename above is distinct, and why
        # nothing here may ever be cleaned up by globbing a directory.
        console.print(f"\n[dim]config and state are the same directory here "
                      f"({CONFIG_DIR}).[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
