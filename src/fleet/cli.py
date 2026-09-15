"""fleet CLI. Every read command supports --json, because the CLI -- not MCP -- is the
universal interface: cron jobs, Makefiles, and non-MCP agents can all use it."""

from __future__ import annotations

import contextlib
import getpass
import json as jsonlib
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import typer
import yaml
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import reconcile as rec
from . import service
from .agents import (MCP_CLIENTS, TARGETS, detect_mcp_clients, detect_targets,
                     fleet_command, fleet_executable, install, install_mcp,
                     package_version, uninstall, uninstall_mcp)
from .config import (CONFIG_DIR, DB_PATH, DEFAULT_PORT, FLEET_KEY, INVENTORY_PATH,
                     STATE_DIR, load_config)
from .edit import apply_edits
from .install import build_install_argv, install_script, payload_for
from .mcpserver import McpUnavailable, serve as serve_mcp
from .models import Device, Kind, Status
from .onboard import onboard, onboard_self
from .ops import FleetError, identity
from .ops import sync as _sync
from .ops.enrol import (finish_add as _enrol_after_add,
                        install_our_key as _install_key,
                        register_identity as _register_identity)
from .ops.handover import accept as _accept_handover, give_away as _handover
from .ops.lifecycle import (dissolve as _dissolve, leave as _leave_fleet,
                            membership as _fleet_membership)
from .ops.migrate import run as _migrate_passwords
from .ops.names import canonical as _canonical
from .ops.rows import snapshot as _rows, tick as _live_tick
from .ops.sweep import (apply_now as _apply_now, broadcast as _broadcast,
                        endpoint_for as _endpoint_for,
                        enrol_unpinned as _enrol_unpinned, run as _sweep)
from .ops.sync import (center_advertise_url, ensure_fresh,
                       file_request as _file_request, post as _post,
                       record_relayed as _record_relayed,
                       telemetry_to_relay as _telemetry_to_relay,
                       this_host as _this_host)
from .probe.runner import PAYLOAD, probe_env, probe_many, run_probe, run_probe_local
from .render import view as view_mod
from .render.staleness import staleness_note
from .render.top import (Schedule, device_lines, disk_cell, gpu_cells_compact,
                         name_cell, render_device, render_fleet)
from .render.view import Detail, auth_of, device_view, fleet_view, matches_tag
from .serve import serve as serve_center
from .ssh.cmd import (build_argv, local_platform, local_shell_argv, remote_command,
                      run as sshrun,
                      remote_platform, resolve_command)
from .ssh.keys import (ensure_keypair, install_key, install_key_over_existing_access,
                       pty_available)
from .state import access as acl
from .state import inventory as inv
from .state import store
from .ui import (DOT as _DOT, chatter_to_stderr as _chatter_to_stderr, console,
                 emit as _emit, err)

app = typer.Typer(
    add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
    help="Personal compute inventory, service registry, and resource broker.",)
@contextmanager
def _as_exit():
    """Turn an operation's failure into an exit code, at the only layer that has them.

    This is the whole point of `FleetError`: the operation says what went wrong and how
    badly, and each surface decides what that means. Here it is an exit status. In
    `serve.py` it is an HTTP code, and in `mcp.py` a tool result -- none of which an
    operation should have to know about.

    Nothing is printed here. The convention is that an operation explains itself as it
    fails -- it has the context to say it well, and it is already mid-sentence with the
    user. The message on the exception is for the surfaces that cannot see that output.
    """
    try:
        yield
    except FleetError as exc:
        raise typer.Exit(exc.code) from None


def _this_machine(devices, what: str):
    """The device record for the machine we are on, or a useful error.

    Lets a name be omitted where "the one I am standing on" is the obvious default. Not
    offered everywhere: `fleet ssh` to yourself is what a terminal already is, and a
    destructive command must never guess which machine it is about.
    """
    me = inv.find(devices, identity.local_device_id()) if identity.local_device_id() else None
    if me is None:
        err.print(f"[red]This machine is not in the inventory,[/red] so there is nothing "
                  f"to {what}.")
        err.print("  [dim]add it with [bold]fleet add --self[/bold][/dim]")
        raise typer.Exit(2)
    return me


@app.command("ls")
def cmd_ls(names: list[str] = typer.Argument(None, help="only these devices"),
           json_out: bool = typer.Option(False, "--json"),
           refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe"),
           online: bool = typer.Option(False, "--online", help="only reachable devices"),
           tag: list[str] = typer.Option(None, "--tag", metavar="NAME",
                                         help="only machines carrying this tag or fact; "
                                              "repeatable, and all must match")):
    """List every device with live resource availability.

    [dim]Example:[/dim]  fleet ls --json
    """
    ensure_fresh()
    rows = _rows(list(names) if names else None, refresh=refresh)
    if online:
        rows = [r for r in rows if r["status"] == "ok"]
    if tag:
        # Say what could not be judged rather than dropping it silently. A machine with no
        # telemetry has no facts, so it fails every filter -- and the fleet is factless
        # right after a cache wipe or a schema bump. Shared hosts are worse: `_rows` only
        # probes `probeable`, and SHARED is forced to on_demand at onboarding because one
        # cluster hangs ~75s, so a bare `fleet ls --tag gpu` judges it on nothing while
        # `fleet ls koa04 --tag gpu` probes and answers differently.
        blind = [r["name"] for r in rows if not r["facts"] and not r["tags"]]
        rows = [r for r in rows if all(matches_tag(r, t) for t in tag)]
        if blind:
            err.print(f"[dim]! {len(blind)} with no telemetry were not considered: "
                      f"{', '.join(sorted(blind)[:6])}[/dim]")
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
    if note := staleness_note():
        console.print(f"[yellow]![/yellow] [dim]{note}[/dim]")
    s = view["summary"]
    console.print(f"\n[dim]{s['online']}/{s['total']} online · {s['gpus_free']} free GPU(s)"
                  + (f" · ${s['hourly_burn']:.2f}/hr burning" if s["hourly_burn"] else "") + "[/dim]")


@app.command("show")
def cmd_show(name: str = typer.Argument(None, help="defaults to this machine"),
             json_out: bool = typer.Option(False, "--json"),
             refresh: bool = typer.Option(True, "--refresh/--no-refresh")):
    """Full detail for one device.

    [dim]Example:[/dim]  fleet show machine_A
    """
    ensure_fresh()
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
    # Both lists, labelled and apart, because this is the one surface with room for them
    # and the one a human reads when deciding. `tags` is what you said; `facts` is what
    # the last probe measured, and the two must stay distinguishable.
    if r.get("tags"):
        console.print(f"  tags  {' '.join(r['tags'])}")
    if r.get("facts"):
        console.print(f"  facts [dim]{' '.join(r['facts'])}[/dim]")
    for a in r["alerts"]:
        console.print(f"  [yellow]![/yellow] {a}")
    c = r["connect"]
    console.print(f"\n  [bold]connect[/bold]  {c.get('ssh_command') or c.get('hint')}")
    if r.get("notes"):
        console.print(f"  [dim]{r['notes'].strip()}[/dim]")


def _tags(values) -> list[str] | None:
    """Normalise a repeatable --tag/--untag into a clean list, or None if unmentioned.

    Lowercased because the request that prompted tags said "GPU", "NAS" and "IP" while
    every derived fact is lowercase -- `--tag GPU` matching nothing would be the first
    thing anyone hit.
    """
    if not values:
        return None
    return [t for t in (view_mod.normalise_tag(v) for v in values) if t] or None




# A host that never answered. Adding it would record an address nobody can reach and a
# name that means nothing, and the first thing anyone did with it would fail. A host key
# that changed belongs here too: it answered, but not as itself.
_DID_NOT_ANSWER = (Status.TIMEOUT, Status.UNREACHABLE, Status.REFUSED, Status.CLOSED,
                   Status.HOST_KEY_MISMATCH)


@app.command("add")
def cmd_add(ssh_command: str = typer.Argument(None, help='e.g. "ssh -p 58418 root@1.2.3.4"'),
            this_machine: bool = typer.Option(False, "--self",
                                              help="record the machine you are on, with no ssh"),
            name: str = typer.Option(None, "--name"),
            alias: str = typer.Option("", "--alias", metavar="SHORT",
                                      help="a short handle to type instead of the name"),
            tag: list[str] = typer.Option(None, "--tag", metavar="NAME",
                                          help="label it; repeatable. `fleet ls --tag NAME` "
                                               "finds it again"),
            kind: str = typer.Option(None, "--kind", help="permanent|rental|shared|appliance|mobile"),
            json_out: bool = typer.Option(False, "--json"),
            dry_run: bool = typer.Option(False, "--dry-run")):
    """Add a machine to the fleet, enrolling it.

    [dim]Example:[/dim]  fleet add "ssh -p 58418 root@1.2.3.4"
    """
    if this_machine == bool(ssh_command):
        err.print("[red]Give an ssh command, or --self -- not both, not neither.[/red]")
        raise typer.Exit(2)
    where = _fleet_membership()
    if not where:
        err.print("[red]This machine is not in a fleet.[/red]")
        err.print("  [dim]start one with [bold]fleet center --init[/bold] — "
                  "a machine is added to a fleet, so the fleet comes first[/dim]")
        raise typer.Exit(2)
    devices = inv.load()
    taken = inv.handles(devices)
    if alias and alias in taken:
        err.print(f"[red]Another machine already answers to {alias!r}.[/red]")
        err.print("  [dim]aliases share the namespace with names, so the short form is "
                  "never ambiguous[/dim]")
        raise typer.Exit(2)
    if this_machine:
        # No ssh at all: `fleet add "ssh localhost"` would need inbound sshd on a laptop,
        # which is the thing run_probe_local exists to avoid, and the center is never an
        # ssh target by design. It still has to be in its own inventory.
        dev, res = onboard_self(name=name, kind=kind, alias=alias, taken_names=taken)
    else:
        dev, res = onboard(ssh_command, name=name, kind=kind, alias=alias,
                           taken_names=taken)
    if res.status in _DID_NOT_ANSWER:
        err.print(f"[red]{dev.name} did not answer[/red] — "
                  f"{res.status.value}: {res.error_detail}")
        err.print("  [dim]nothing recorded. A machine has to be reachable to be "
                  "managed at all: the center installs and removes keys over ssh, so "
                  "one it cannot dial cannot be granted or revoked anything.[/dim]")
        raise typer.Exit(1)
    if dry_run:
        _emit({"device": dev.name, "id": dev.id, "kind": dev.kind.value,
               "status": res.status.value}, True)
        return
    devices, action = inv.upsert(devices, dev)
    if tags := _tags(tag):
        # `upsert` matches on id, merges only endpoints and discards every other field of
        # the incoming record, so on a machine already known -- re-adding a rental whose
        # port moved, say -- the tags would be dropped without a word. Apply them to the
        # record that actually survived.
        if survivor := inv.find_exact(devices, dev.id):
            apply_edits(survivor, add_tags=tags)
            for t in tags:
                if view_mod.is_fact_name(t):
                    console.print(f"  [dim]note: {t!r} is also derived from telemetry — "
                                  "kept, since a probe cannot see everything[/dim]")
    inv.save(devices)
    if res.snapshot is not None or not res.ok:
        conn = store.connect()
        store.record(conn, dev.id, res)
        conn.close()
    if not json_out:
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

    # --json still enrols. An agent uses --json, and a flag that quietly did half the
    # command would be the worst kind of difference -- but enrolment talks, and that talk
    # must not land in the middle of the document.
    with _chatter_to_stderr(json_out):
        outcome = _enrol_after_add(dev, res, where=where, this_machine=this_machine)
    _emit({"action": action, "name": dev.name, "id": dev.id, "kind": dev.kind.value,
           "status": res.status.value, "enrolment": outcome}, json_out)
    if outcome == "failed":
        raise typer.Exit(1)








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
             new_alias: str = typer.Option(None, "--alias", metavar="SHORT",
                                           help="a short handle to type instead of the "
                                                'name; --alias "" removes it'),
             tag: list[str] = typer.Option(None, "--tag", metavar="NAME",
                                           help="add a label; repeatable"),
             untag: list[str] = typer.Option(None, "--untag", metavar="NAME",
                                             help="remove a label; repeatable"),
             role: str = typer.Option(None, "--role", help="none | center | backup"),
             json_out: bool = typer.Option(False, "--json")):
    """Change a device's address or settings after it was added.

    Rentals recycle IPs and ports, so `--ssh` re-points a device without losing its
    name, tags, cost or history.

    [dim]Example:[/dim]  fleet edit machine_A --ssh "ssh -p 40001 root@5.6.7.8"
    """
    devices = inv.load()
    dev = _this_machine(devices, "edit") if name is None else inv.find(devices, name)
    if dev is None:
        err.print(f"[red]No device named {name!r}[/red]")
        raise typer.Exit(1)

    endpoint = resolve_command(ssh_command) if ssh_command else None
    paths = [] if clear_disk_paths else (list(disk_path) if disk_path else None)
    result = apply_edits(dev, name=new_name, alias=new_alias,
                         add_tags=_tags(tag), drop_tags=_tags(untag),
                         taken=inv.handles(devices, excluding=dev.id),
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


def run_installer(ep, script: str, *, forward_agent: bool = True,
                  platform: str = "") -> tuple[int, str]:
    argv = build_install_argv(ep, forward_agent=forward_agent, platform=platform)
    p = sshrun(argv, input=payload_for(script, platform), timeout=900)
    return p.returncode, (p.stdout + p.stderr).decode(errors="replace")


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

    [dim]Example:[/dim]  fleet install machine_A
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

    # Which shell the far side speaks, from the last probe. An unprobed machine reads
    # POSIX, which is the safe way to be wrong: a POSIX script on Windows fails loudly,
    # where the reverse can appear to succeed.
    conn = store.connect()
    try:
        _, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    platform = remote_platform(snap)

    console.print(f"[dim]installing fleet on {dev.name} from {url} ({ref})[/dim]")
    code, output = run_installer(sorted(eps, key=lambda e: e.preference)[0],
                                 install_script(url, ref=ref, platform=platform),
                                 forward_agent=forward_agent, platform=platform)
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




def _show_version(value: bool):
    if value:
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

    me = identity.local_device_id()
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
             from_url: str = typer.Option(None, "--from", metavar="URL",
                                          help="dial a center at this address and "
                                               "remember it, when it cannot reach you"),
             json_out: bool = typer.Option(False, "--json")):
    """Merge this machine's inventory with the center's.

    Safe to run anywhere and repeatedly: the merge is a union, the newer record wins,
    and a device known to only one side is never dropped.

    [dim]Example:[/dim]  fleet sync
    """
    if from_url:
        # Before the center check: this is for a machine that cannot be reached *by* the
        # center, and it is the only way in for one that has never been swept.
        with _as_exit():
            summary = _sync.join(from_url)
        console.print(f"[green]✓[/green] {summary}")
        return

    if serve:

        raw = sys.stdin.read()
        # The inventory carries the endpoints that decide where `fleet ssh` dials, and
        # this filter runs on a spoke that every granted peer holds a key for. Verifying
        # the access list and taking the routing on trust would have secured the policy
        # and left the routes open, so the whole envelope is checked.
        pinned = acl.trusted_center_pubkey()
        relayed = []
        url = ""
        try:
            if pinned:
                note = acl.unseal(raw, pinned)
                body, relayed, url = note["inventory"], note["telemetry"], note["center_url"]
            else:
                body = acl.unseal_first_contact(raw)
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        acl.note_center_seen()
        # Learned here, over a payload already signed by the key this machine pinned at
        # enrolment. After this it can refresh itself and stop waiting to be swept.
        acl.note_center_url(url)
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

    # Whether this machine is the center is settled by the access list -- possession of
    # the signing key -- not by `Device.role`. role rides `inventory.merge`, where a peer
    # with a fast clock could flip it, and comparing `identity.local_device_id()` adds a third way
    # to be wrong: it shells out to `ioreg` on macOS, which is not on cron's PATH, and
    # returns nothing at all on Windows. Ask the one authority.
    try:
        if acl.is_center(acl.load()):
            with _as_exit():
                _sweep(devices)
            return
    except acl.AccessError:
        pass                               # no access list here: a spoke, or no fleet yet

    center = next((d for d in inv.live(devices) if d.role == "center"), None)
    if center is None:
        err.print("[red]This machine is not in a fleet, and no center is recorded.[/red]")
        err.print("  [dim]start one here with [bold]fleet center --init[/bold], or let "
                  "the center reach this machine once[/dim]")
        raise typer.Exit(2)
    if center.id and center.id == identity.local_device_id():
        # An inventory that predates the access list, where `role` was the only answer.
        # Kept so `fleet sync` on such a center stays a no-op rather than dialling itself.
        with _as_exit():
            _sweep(devices)
        return
    eps = inv.endpoints_of(center)
    if not eps:
        err.print(f"[red]{center.name} is the center but has no endpoint recorded[/red]")
        raise typer.Exit(2)

    code, output = _sync.run_sync(sorted(eps, key=lambda e: e.preference)[0], inv.dumps(devices))
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


@app.command("top")
def cmd_top(name: str = typer.Argument(None, help="one device, instead of the whole fleet"),
            interval: float = typer.Option(2.0, "--interval", "-i",
                                           help="seconds between refreshes")):
    """Live view of the fleet, or of one device. Like htop, for your machines.

    Shared hosts keep their own slow cadence (shared_min_interval_s) and are shown as
    ageing rather than live, and anything unreachable backs off instead of being
    redialled every couple of seconds.

    [dim]Example:[/dim]  fleet top machine_A -i 1
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

    [dim]Example:[/dim]  fleet rm machine_A
    """
    devices = inv.load()
    # Exact only. Removing revokes keys everywhere and cannot be undone by re-running,
    # so it must never act on a prefix someone half-remembered.
    dev = inv.find_exact(devices, name)
    if dev is None:
        err.print(f"[red]No machine named exactly {name!r}[/red]")
        if near := inv.near_matches(devices, name):
            err.print(f"  [dim]did you mean: {', '.join(near)}[/dim]")
        raise typer.Exit(1)

    # Removing a machine revokes its keys everywhere, which only the center can do.
    # Removing *yourself* is a different act -- leaving -- and needs nobody's permission,
    # because you own the machine you are standing on.
    itself = bool(dev.id) and dev.id == identity.local_device_id()
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

    [dim]Example:[/dim]  fleet probe machine_A --raw
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
        argv = build_argv(sorted(eps, key=lambda e: e.preference)[0],
                          remote="sh -s", env=probe_env(dev.probe_mode, dev.disk_paths))
        p = sshrun(argv, input=PAYLOAD.read_bytes())
        sys.stdout.write(p.stdout.decode(errors="replace"))
        raise typer.Exit(0 if p.returncode == 0 else 1)
    res = run_probe(sorted(eps, key=lambda e: e.preference)[0], mode=dev.probe_mode,
                    disk_paths=dev.disk_paths)
    console.print_json(jsonlib.dumps(
        {"status": res.status.value, "latency_ms": res.latency_ms,
         "error": res.error_detail, "snapshot": res.snapshot.to_dict() if res.snapshot else None},
        default=str))


@app.command("ssh", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def cmd_ssh(ctx: typer.Context, name: str):
    """Open a shell on a device, or run a command: `fleet ssh machine_A -- nvidia-smi`.

    This exists so credentials never have to reach an agent: the wrapper resolves the
    endpoint and connects, rather than handing out a connection string plus a password.

    [dim]Example:[/dim]  fleet ssh machine_A -- nvidia-smi
    """
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
    platform = remote_platform(snap)
    if auth_of(dev, cached) == "needs_key":
        err.print(f"[yellow]{dev.name} rejected our key.[/yellow] Only the center can "
                  "install one:")
        err.print(f"  [bold]fleet add \"ssh ...\"[/bold] on the center, or "
                  "[bold]fleet sync[/bold] if it is already recorded")
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
        argv.append(remote_command(extra, windows=platform == "windows"))

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

    [dim]Example:[/dim]  fleet access machine_A --allow machine_B
    """

    if migrate:
        with _as_exit():
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
            dst = acl.resolve(current, _canonical(target))
            src = acl.resolve(current, _canonical(allow or deny))
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
        if changed:
            # Applied here rather than left for a sweep. You have just said what you
            # want, so telling you to run a second command to mean it was always a poor
            # trade -- and for a revoke it is worse than that: a machine that waits to
            # be asked would keep the key until it next happened to sync, which for an
            # idle machine is never, while the peer losing access carries on using it.
            _apply_now(current, src, dst, user, install=bool(allow))

    ledger = rec.load_ledger()
    rows = []
    for edge in sorted(current.edges()):
        src, dst, who = edge
        if target and dst != acl.resolve(current, _canonical(target)):
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




@app.command("center")
def cmd_center(name: str = typer.Argument(None, help="hand the role to this machine"),
               init: bool = typer.Option(False, "--init",
                                         help="start a fleet with this machine as center"),
               pubkey: bool = typer.Option(False, "--pubkey",
                                           help="print the key to pre-place on a host"),
               export: bool = typer.Option(False, "--export",
                                           help="print the access list and pins"),
               listen: bool = typer.Option(False, "--listen",
                                           help="serve the fleet so machines can sync "
                                                "themselves, instead of being swept"),
               no_service: bool = typer.Option(False, "--no-service",
                                               help="with --init: do not install the "
                                                    "background service"),
               port: int = typer.Option(0, "--port", metavar="N",
                                        help="port for --listen (default 7373)"),
               advertise: str = typer.Option("", "--advertise", metavar="URL",
                                             help="the address machines should dial; "
                                                  "defaults to this host and port"),
               accept: bool = typer.Option(False, "--accept",
                                           help="take the role a handover offered"),
               dissolve: bool = typer.Option(False, "--dissolve",
                                             help="take the whole fleet down: remove every "
                                                  "key from every machine"),
               leave: bool = typer.Option(False, "--leave",
                                          help="remove this fleet's keys from this machine"),
               json_out: bool = typer.Option(False, "--json"),
               force: bool = typer.Option(False, "--force")):
    """Who decides, and handing that over.

    Only the current center can name the next one. No machine may promote itself, so an
    unplanned loss of the center means re-configuring by hand -- which is the price of
    there being exactly one machine that can open every door.

    [dim]Example:[/dim]  fleet center machine_B
    """

    if pubkey:
        # Deliberately works with nothing reachable and no inventory: the moment you
        # want this is before the machine exists, writing a cloud-init file.
        _, pub = ensure_keypair()
        print(pub)
        return

    if listen:

        try:
            acc = acl.load()
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        if not acl.is_center(acc):
            err.print("[red]Only the center can serve the fleet.[/red]")
            raise typer.Exit(2)
        where = port or DEFAULT_PORT
        url = advertise or center_advertise_url(acc, where)
        console.print(f"[green]✓[/green] serving fleet {acc.fleet_id} on port {where}")
        console.print(f"  [dim]machines are told to dial {url}[/dim]")
        console.print("  [dim]only keys this fleet has pinned are answered; "
                      "first contact still happens by enrolment[/dim]")
        try:
            serve_center(port=where, advertise=url)
        except KeyboardInterrupt:
            console.print("\n[dim]stopped[/dim]")
        return

    if init:
        key_path, pub = ensure_keypair()
        devices = inv.load()
        dev, res = onboard_self()
        # Re-running --init must not rename this machine. Passing every existing name as
        # taken counted its *own* record among them, so a second --init came back as
        # "<name>-2" and pinned that into the access list while the inventory kept the
        # first -- the exact name split seeding both from one object exists to prevent.
        if existing := inv.find_exact(devices, dev.id):
            dev.name = existing.name
        else:
            taken, base, n = inv.handles(devices), dev.name, 2
            while dev.name in taken:
                dev.name, n = f"{base}-{n}", n + 1
        try:
            acc = acl.bootstrap(dev.name, pub, dev.id)
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        # Reflect the role in the inventory too. `is_center()` remains the authority --
        # this field rides the merge and cannot be trusted for a decision -- but it is
        # what `ls` and `top` draw the diamond from, and a center nobody can see in the
        # table is the problem the glyph was added to solve.
        dev.role = "center"
        # Seed the inventory from the same object the access list was pinned from. Done
        # separately the two derive a name each, and nothing reconciles them: the access
        # list would keep answering to one name while `fleet show` knew the other. It
        # also spares the user a `fleet add --self` they have no way to know they need.
        devices, _ = inv.upsert(devices, dev)
        inv.save(devices)
        if res.snapshot is not None:
            conn = store.connect()
            store.record(conn, dev.id, res)
            conn.close()
        console.print(f"[green]✓[/green] fleet {acc.fleet_id} started; "
                      f"{dev.name} is the center.")
        if not no_service:
            # Installed here rather than left as a step to remember: machines keep
            # themselves current by asking the center, and a center that only listens
            # while someone holds a terminal open is not one they can ask.

            console.print(f"  [dim]service: {service.install(fleet_executable(), DEFAULT_PORT)}[/dim]")
        console.print(f"  [dim]key to pre-place on locked-down hosts: "
                      f"[bold]fleet center --pubkey[/bold][/dim]")
        return

    try:
        acc = acl.load()
    except acl.AccessError as exc:
        err.print(f"[red]{exc}[/red]")
        err.print("  [dim]start one with [bold]fleet center --init[/bold][/dim]")
        raise typer.Exit(2)

    if dissolve:
        with _as_exit():
            _dissolve(acc, force=force)
        return
    if accept:
        with _as_exit():
            _accept_handover(acc)
        return
    if leave:
        _leave_fleet(acc)
        return
    if export:
        print(acl.dumps(acc))
        return
    if name:
        with _as_exit():
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
    if centre:
        # Worth a line: machines keep themselves current by asking this one, so a
        # service that is not up means the whole fleet quietly goes stale, and nothing
        # else on this page would say so.

        state = service.status()
        if state == "running":
            console.print(f"serving  {center_advertise_url(acc)}")
        elif state == "installed":
            console.print("[yellow]![/yellow] the service is installed but not running "
                          "-- machines cannot refresh themselves")
        else:
            console.print("[yellow]![/yellow] not serving; machines wait to be swept "
                          "-- [bold]fleet service install[/bold]")
    if note := staleness_note():
        console.print(f"[yellow]![/yellow] {note}")
    if not centre:
        console.print("\n[dim]Changes are made on the center. Losing it means "
                      "re-configuring by hand -- keep a copy: [bold]fleet center "
                      "--export[/bold][/dim]")












@app.command("setup")
def cmd_setup(
    target: str = typer.Option("auto", "--target",
                               help="an agent name, or all | auto (whatever is installed)"),
    project: bool = typer.Option(False, "--project",
                                 help="write into the current directory, not your home"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would change; write nothing"),
    remove: bool = typer.Option(False, "--uninstall", help="remove what setup installed"),
):
    """Teach your coding agents to use fleet.

    An agent with a shell gets a skill, which costs nothing until a task actually needs
    a machine. A desktop client has no shell, so it gets `fleet mcp` registered as an
    MCP server instead -- merged into its config beside whatever else is already there.

    [dim]Example:[/dim]  fleet setup --dry-run
    """
    root = Path.cwd() if project else Path.home()
    # MCP clients are a second namespace: a desktop app is registered, not written to.
    # `--project` never touches them -- their config is per-user, not per-repo.

    mcp_names = tuple(c.name for c in MCP_CLIENTS)
    if target == "auto":
        # cwd tells us nothing about which agents you use, so a project install
        # assumes the one whose layout is identical in both scopes.
        targets = ["claude"] if project else detect_targets(root)
        clients = [] if project else detect_mcp_clients(root)
    elif target == "all":
        targets = list(TARGETS)
        clients = [] if project else list(mcp_names)
    else:
        targets = [target] if target in TARGETS else []
        clients = [target] if target in mcp_names else []

    if unknown := [t for t in [target] if t not in (*TARGETS, *mcp_names, "all", "auto")]:
        err.print(f"[red]Unknown target {unknown[0]!r}[/red]  "
                  f"({' | '.join((*TARGETS, *mcp_names))} | all | auto)")
        raise typer.Exit(2)
    if not targets and not clients:
        err.print("[yellow]No coding agent found.[/yellow]  Looked for "
                  f"{', '.join('~/.' + t for t in TARGETS)} and the desktop clients.  "
                  "Force one with [bold]--target claude[/bold].")
        raise typer.Exit(1)

    cmd = fleet_command()
    if remove:
        changes = (uninstall(root, targets, dry_run=dry_run, project=project)
                   + uninstall_mcp(root, clients, dry_run=dry_run))
    else:

        changes = (install(root, targets, cmd, dry_run=dry_run, project=project)
                   + install_mcp(root, clients, fleet_executable(), dry_run=dry_run))

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


@app.command("service", hidden=True)
def cmd_service(action: str = typer.Argument("status",
                                             help="status | install | remove | start | stop")):
    """Manage the background service that keeps the center listening.

    Hidden because nobody should have to run it: `fleet center --init` installs it and
    `fleet update` stops and starts it around the install. It exists so those two have
    something to call, and so you can look when something is wrong.

    [dim]Example:[/dim]  fleet service status
    """

    if action == "status":
        console.print(service.status())
        return
    if action == "install":

        try:
            acc = acl.load()
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        console.print(service.install(fleet_executable(), DEFAULT_PORT))
        console.print(f"  [dim]machines will dial {center_advertise_url(acc)}[/dim]")
        return
    if action == "remove":
        console.print(service.remove())
        return
    if action in ("start", "stop"):
        getattr(service, action)()
        return
    err.print(f"[red]Unknown action {action!r}[/red]  (status | install | remove | start | stop)")
    raise typer.Exit(2)


@app.command("mcp", hidden=True)
def cmd_mcp():
    """Serve fleet over MCP on stdio, for agents that cannot run a shell.

    Hidden because nobody types it: a desktop client launches it, from a config entry
    `fleet setup` writes. Everything it exposes is a command on this page.

    [dim]Example:[/dim]  fleet mcp
    """

    try:
        serve_mcp()
    except McpUnavailable as exc:
        # escaped: the message names an extra, and `[mcp]` is rich markup -- unescaped
        # it printed the install command with the extra silently removed, which is the
        # one part of it the reader needs.

        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)


@app.command("paths")
def cmd_paths():
    """Show where fleet keeps its state.

    [dim]Example:[/dim]  fleet paths
    """

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
