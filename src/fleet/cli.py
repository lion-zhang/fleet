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
from .keys import install_key, public_key
from .models import Device, Kind, Status
from .onboard import onboard, onboard_self
from .probe.runner import (probe_env, probe_many, run_probe, run_probe_local,
                           run_probe_with_password)
from . import secrets as sec
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


def _emit(payload, as_json: bool) -> bool:
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))
    return as_json


def stored_password(name: str) -> str | None:
    """A password for this device, if one is stored and this machine can read it.

    Best-effort on purpose: no identity, no secrets file, or not being an enrolled
    recipient are all ordinary states, and none of them may stop `fleet ls` working.
    """
    try:
        return sec.read_secrets(sec.SECRETS_PATH, sec.load_identity()).get(name)
    except Exception:
        return None


def _needs_password(conn, devices) -> set[str]:
    """Devices whose last probe says the host is up but rejected our key."""
    out = set()
    for d in devices:
        st, _ = store.latest(conn, d.id)
        if st and st.get("status") == Status.AUTH_FAILED.value:
            out.add(d.name)
    return out


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
        rejected = _needs_password(conn, stale)
        for d in stale:
            if d.name not in rejected:
                continue
            password = stored_password(d.name)
            if not password:
                continue
            eps = inv.endpoints_of(d)
            if not eps:
                continue
            res = run_probe_with_password(sorted(eps, key=lambda e: e.preference)[0],
                                          password, mode=d.probe_mode,
                                          disk_paths=d.disk_paths)
            store.record(conn, d.id, res)

    out = []
    self_id = local_device_id()
    for d in devices:
        st, sn = store.latest(conn, d.id)
        out.append(device_view(d, st, sn, detail, self_id=self_id))
    conn.close()
    return out


@app.command("ls")
def cmd_ls(json_out: bool = typer.Option(False, "--json"),
           refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe"),
           online: bool = typer.Option(False, "--online", help="only reachable devices")):
    """List every device with live resource availability.

    [dim]Example:[/dim]  fleet ls --json
    """
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
def cmd_show(name: str, json_out: bool = typer.Option(False, "--json"),
             refresh: bool = typer.Option(True, "--refresh/--no-refresh")):
    """Full detail for one device.

    [dim]Example:[/dim]  fleet show lin-xps
    """
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
                console.print(f"  [dim]run [bold]fleet key install {dev.name}[/bold] from a "
                              "terminal to install your key.[/dim]")
            elif typer.confirm(f"  Install your public key on {dev.name} now?", default=True):
                if _install_key(dev):
                    inv.save(devices)


key_app = typer.Typer(no_args_is_help=True,
                      help="Install your SSH key on a device so password auth is not needed.")
app.add_typer(key_app, name="key")


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
    found = public_key()
    if found is None:
        err.print("[red]No SSH public key found.[/red]  Create one first:  "
                  "[bold]ssh-keygen -t ed25519[/bold]")
        return False
    if not sys.stdin.isatty():
        # Hanging on a prompt would be bad; capturing the password into whatever called
        # us would be worse. Refuse, and say exactly what to run instead.
        msg = ("  [dim]no terminal here — run [bold]fleet key install "
               f"{dev.name}[/bold] yourself to install your key.[/dim]")
        (console if quiet else err).print(msg)
        return False

    ep = sorted(eps, key=lambda e: e.preference)[0]
    path, pubkey = found
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


@key_app.command("install")
def cmd_key_install(name: str):
    """Install your public key on a device, using a password typed once.

    [dim]Example:[/dim]  fleet key install ds720
    """
    devices = inv.load()
    dev = inv.find(devices, name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)
    if not _install_key(dev):
        raise typer.Exit(2)
    inv.save(devices)


@app.command("edit")
def cmd_edit(name: str,
             ssh_command: str = typer.Option(None, "--ssh", metavar="CMD",
                                             help='new address, e.g. "ssh -p 2222 root@5.6.7.8"'),
             disk_path: list[str] = typer.Option(None, "--disk-path", metavar="PATH",
                                                 help="watch this mount or directory for free "
                                                      "space; repeatable"),
             clear_disk_paths: bool = typer.Option(False, "--clear-disk-paths",
                                                   help="go back to autodetecting mounts"),
             role: str = typer.Option(None, "--role", help="none | center | backup"),
             json_out: bool = typer.Option(False, "--json")):
    """Change a device's address or settings after it was added.

    Rentals recycle IPs and ports, so `--ssh` re-points a device without losing its
    name, tags, cost or history.

    [dim]Example:[/dim]  fleet edit blackwell --ssh "ssh -p 40001 root@5.6.7.8"
    """
    devices = inv.load()
    dev = inv.find(devices, name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)

    endpoint = resolve_command(ssh_command) if ssh_command else None
    paths = [] if clear_disk_paths else (list(disk_path) if disk_path else None)
    result = apply_edits(dev, endpoint=endpoint, disk_paths=paths,
                         role=None if role == "center" else role)
    if role == "center":
        # fleet-wide invariant, so it cannot live in apply_edits, which sees one device
        result.changes += inv.promote_center(devices, dev)

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
def cmd_install(name: str,
                repo: str = typer.Option(None, "--repo", metavar="URL",
                                         help="git URL to clone; defaults to config or this checkout"),
                ref: str = typer.Option("main", "--ref", help="branch or tag to install"),
                role: str = typer.Option(None, "--role",
                                         help="none | center | backup "
                                              "(default: keep, or backup if unset)"),
                forward_agent: bool = typer.Option(True, "--forward-agent/--no-forward-agent",
                                                   help="authenticate the clone as you, "
                                                        "leaving no credential on the device")):
    """Install fleet on a device so it can hold a copy of your state.

    Every other device needs nothing installed. This is the exception: a backup node has
    to run fleet, so fleet has to be there. Re-running updates an existing install.

    [dim]Example:[/dim]  fleet install oracle
    """
    devices = inv.load()
    dev = inv.find(devices, name)
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
    if role is None:
        role = dev.role if dev.role != "none" else "backup"
    if role == "center":
        inv.promote_center(devices, dev)    # one center, enforced in one place
    elif role != dev.role:
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
    p = subprocess.run(argv, input=acl.seal(payload), capture_output=True, text=True,
                       timeout=180)
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
        try:
            body = acl.unseal(raw, pinned) if pinned else acl.unseal_first_contact(raw)
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
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
        console.print("[dim]· this machine is the center; nothing to sync to.[/dim]")
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
    merged, changes = inv.update(lambda current: inv.merge(current, returned))
    if _emit({"center": center.name, "devices": len(merged), "changes": changes}, json_out):
        return
    console.print(f"[green]✓[/green] synced with [bold]{center.name}[/bold] "
                  f"({len(merged)} devices)")
    for line in changes:
        console.print(f"  {line}")
    if not changes:
        console.print("  [dim]already up to date.[/dim]")


@app.command("identity")
def cmd_identity():
    """Enrol this machine so it can read encrypted secrets.

    Creates an age keypair if there is none and publishes only the public half onto this
    machine's own device record, where it syncs like everything else. The private half
    never leaves this machine and is never printed.

    [dim]Example:[/dim]  fleet identity
    """
    try:
        recipient = sec.ensure_identity()
    except sec.SecretsError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    devices = inv.load()
    me = next((d for d in inv.live(devices) if d.id and d.id == local_device_id()), None)
    if me is None:
        err.print("[red]This machine is not in the inventory[/red], so there is nowhere "
                  "to publish its recipient.\n  Add it first:  [bold]fleet add "
                  "\"ssh localhost\"[/bold]")
        raise typer.Exit(2)
    if me.recipient == recipient:
        console.print(f"[dim]· {me.name} is already enrolled.[/dim]")
        return
    me.recipient = recipient
    inv.touch(me)
    inv.save(devices)
    console.print(f"[green]✓[/green] {me.name} enrolled as [bold]{recipient}[/bold]")
    console.print("  [dim]re-run `fleet secret set` for existing secrets to include "
                  "this machine.[/dim]")


secret_app = typer.Typer(
    no_args_is_help=True, rich_markup_mode="rich",
    help="Stored passwords, encrypted per machine.",)
app.add_typer(secret_app, name="secret")


def _secrets_now() -> tuple[dict, list[Device]]:
    devices = inv.load()
    return sec.read_secrets(sec.SECRETS_PATH, sec.load_identity()), devices


@secret_app.command("set")
def cmd_secret_set(name: str):
    """Store a password for a device. Prompted for, never passed as an argument.

    [dim]Example:[/dim]  fleet secret set blackwell
    """
    if not sys.stdin.isatty():
        err.print("[yellow]Refusing to read a password without a terminal.[/yellow]  "
                  f"Run [bold]fleet secret set {name}[/bold] yourself.")
        raise typer.Exit(2)
    try:
        data, devices = _secrets_now()
        value = getpass.getpass(f"Password for {name}: ")
        data[name] = value
        sec.write_secrets(sec.SECRETS_PATH, data, sec.recipients_of(devices))
    except sec.SecretsError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    finally:
        value = ""
    console.print(f"[green]✓[/green] stored a password for [bold]{name}[/bold], readable "
                  f"by {len(sec.recipients_of(devices))} enrolled machine(s).")


@secret_app.command("ls")
def cmd_secret_ls(json_out: bool = typer.Option(False, "--json")):
    """List which devices have a stored password. Never prints a value.

    [dim]Example:[/dim]  fleet secret ls
    """
    try:
        data, _ = _secrets_now()
    except sec.SecretsError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if _emit({"secrets": sorted(data)}, json_out):
        return
    if not data:
        console.print("[dim]No stored passwords.[/dim]")
        return
    for name in sorted(data):
        console.print(f"  {name}")


@secret_app.command("rm")
def cmd_secret_rm(name: str):
    """Forget a stored password.

    [dim]Example:[/dim]  fleet secret rm blackwell
    """
    try:
        data, devices = _secrets_now()
        if name not in data:
            err.print(f"[yellow]No stored password for {name!r}[/yellow]")
            raise typer.Exit(1)
        del data[name]
        sec.write_secrets(sec.SECRETS_PATH, data, sec.recipients_of(devices))
    except sec.SecretsError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    console.print(f"[green]✓[/green] forgot the password for [bold]{name}[/bold]")


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
    if not yes and not typer.confirm(f"Remove {dev.name} ({dev.kind.value})?"):
        raise typer.Exit(1)
    inv.remove(devices, dev)
    inv.save(devices)
    console.print(f"[green]✓[/green] removed {dev.name}")


@app.command("refresh")
def cmd_refresh(names: list[str] = typer.Argument(None), json_out: bool = typer.Option(False, "--json")):
    """Force a live probe of some or all devices.

    [dim]Example:[/dim]  fleet refresh lin-xps
    """
    rows = _rows(list(names) if names else None, refresh=True)
    if _emit(fleet_view(rows), json_out):
        return
    for r in rows:
        console.print(f"{_DOT.get(r['status'],'?')} {r['name']:<16} {r['status']:<16} "
                      f"{r['telemetry_age_s']}s ago")


@app.command("probe")
def cmd_probe(name: str, raw: bool = typer.Option(False, "--raw", help="print payload stdout")):
    """Probe one device directly. --raw captures a new parser test fixture.

    [dim]Example:[/dim]  fleet probe lin-xps --raw
    """
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
        cached, _ = store.latest(conn, dev.id)
    finally:
        conn.close()
    if auth_of(dev, cached) == "needs_key":
        err.print(f"[yellow]{dev.name} rejected our key.[/yellow] Install one:")
        err.print(f"  [bold]fleet key install {dev.name}[/bold]")
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
        argv.append(remote_command(extra))

    os.execvp("ssh", argv)      # replace this process; ssh owns the tty from here


@app.command("access")
def cmd_access(target: str = typer.Argument(None, help="one machine, instead of all"),
               allow: str = typer.Option(None, "--allow", metavar="MACHINE",
                                         help="let MACHINE reach the target"),
               deny: str = typer.Option(None, "--deny", metavar="MACHINE",
                                        help="stop MACHINE reaching the target"),
               user: str = typer.Option("root", "--user", help="whose authorized_keys"),
               json_out: bool = typer.Option(False, "--json")):
    """Who may reach what, and change it.

    Granting installs a key; revoking removes one. Both are things the center does to a
    machine over ssh, so both can be pending -- and a revoke that has not reached its
    target is reported as not in effect, never as done.

    [dim]Example:[/dim]  fleet access oracle --allow lin-xps
    """
    from . import access as acl
    from . import reconcile as rec

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
    console.print(f"inventory  {INVENTORY_PATH}")
    console.print(f"cache      {DB_PATH}   [dim](disposable — delete and re-probe)[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
