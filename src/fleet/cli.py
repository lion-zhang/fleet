"""fleet CLI. Every read command supports --json, because the CLI -- not MCP -- is the
universal interface: cron jobs, Makefiles, and non-MCP agents can all use it."""

from __future__ import annotations

import contextlib
import copy
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

from . import config as _cfg
from . import reconcile as rec
from . import service
from .agents import (MCP_CLIENTS, TARGETS, config_command, detect_mcp_clients,
                     detect_targets, fleet_command, fleet_executable, install,
                     install_mcp, installed_mcp_clients, installed_targets, package_version,
                     stale_mcp_clients, stale_targets, uninstall, uninstall_mcp)
from .config import DEFAULT_PORT, INVENTORY_PATH, load_config
from .edit import apply_edits
from .install import (NOTHING_TO_UPDATE, build_install_argv, install_script,
                      install_source,
                      local_install_argv, payload_for)
from .mcpserver import McpUnavailable, serve as serve_mcp
from .models import Device, Kind, Status
from .onboard import derive_id, onboard, onboard_self
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
                        remove_now as _remove_now,
                        settle_center_edge as _settle_center_edge,
                        endpoint_for as _endpoint_for,
                        enrol_unpinned as _enrol_unpinned, run as _sweep)
from .ops.sync import (center_advertise_url, ensure_fresh,
                       post as _post,
                       record_relayed as _record_relayed,
                       telemetry_to_relay as _telemetry_to_relay,
                       this_host as _this_host)
from .probe.runner import PAYLOAD, probe_env, probe_many, run_probe, run_probe_local
from .render import view as view_mod
from .render.staleness import staleness_note
from .render.top import (Schedule, device_lines, disk_cell, gpu_cells_compact,
                         name_cell, render_device, render_fleet, render_ls)
from .render.view import Detail, auth_of, device_view, fleet_view, matches_tag
from .serve import serve as serve_center
from .ssh.cmd import (WINDOWS, build_argv, local_platform, local_shell_argv, remote_command,
                      run as sshrun,
                      remote_platform, resolve_command)
from .ssh.keys import (ensure_keypair, install_key, install_key_over_existing_access,
                       pty_available)
from .state import access as acl
from .state import inventory as inv
from .state import clock, store
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


def _first_run(command: str) -> None:
    """On a machine in no fleet, start one here and teach the agents. See ops.firstrun."""
    from .ops import firstrun

    firstrun.maybe(command, lambda line: err.print(f"[green]✓[/green] {line}"))


@app.command("ls")
def cmd_ls(names: list[str] = typer.Argument(None, help="only these devices"),
           json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
           refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe"),
           online: bool = typer.Option(False, "--online", help="only reachable devices"),
           tag: list[str] = typer.Option(None, "--tag", metavar="NAME",
                                         help="only machines carrying this tag or fact; "
                                              "repeatable, and all must match")):
    """List every device with live resource availability.

    [dim]Example:[/dim]  fleet ls --json
    """
    _first_run("ls")
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

    t = render_ls(rows)
    console.print(t)
    if note := staleness_note():
        console.print(f"[yellow]![/yellow] [dim]{note}[/dim]")
    if note := _skills_note():
        console.print(f"[yellow]![/yellow] [dim]{note}[/dim]")
    s = view["summary"]
    console.print(f"\n[dim]{s['online']}/{s['total']} online · {s['gpus_free']} free GPU(s)"
                  + (f" · ${s['hourly_burn']:.2f}/hr burning" if s["hourly_burn"] else "") + "[/dim]")


@app.command("show")
def cmd_show(name: str = typer.Argument(None, help="defaults to this machine"),
             json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
             refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="probe it now; --no-refresh shows the last reading")):
    """Full detail for one device.

    [dim]Example:[/dim]  fleet show machine_A
    """
    _first_run("show")
    ensure_fresh()
    unrecorded = False
    if name is None:
        me = identity.local_device_id()
        if me and inv.find(inv.load(), me) is None and _fleet_membership() == "":
            # Nothing set up yet. The first command anyone runs -- often through
            # `uvx`, before installing anything -- used to answer "not in the
            # inventory, so there is nothing to show". Show the machine anyway: one
            # local probe, kept in the disposable cache, the inventory left alone.
            r, unrecorded = _show_unrecorded_self(), True
        else:
            name = _this_machine(inv.load(), "show").name
    if not unrecorded:
        rows = _rows([name], refresh=refresh, detail=Detail.FULL)
        if not rows:
            err.print(f"[red]No device named {name!r}.[/red]  Try [bold]fleet ls[/bold]")
            raise typer.Exit(1)
        r = rows[0]
    if _emit(r, json_out):
        return
    _print_show(r)
    if unrecorded:
        console.print("\n  [dim]this machine is not in a fleet yet: [bold]fleet center "
                      "--init[/bold] starts one here, [bold]fleet add --self[/bold] just "
                      "records it[/dim]")


def _show_unrecorded_self() -> dict:
    from .render.view import device_view

    dev, res = onboard_self()
    conn = store.connect()
    try:
        store.record(conn, dev.id, res)
        state, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    return device_view(dev, state, snap, Detail.FULL, self_id=dev.id)


def _print_show(r: dict) -> None:
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
            name: str = typer.Option(None, "--name", help="what to call it; guessed from the host if omitted"),
            alias: str = typer.Option("", "--alias", metavar="SHORT",
                                      help="a short handle to type instead of the name"),
            tag: list[str] = typer.Option(None, "--tag", metavar="NAME",
                                          help="label it; repeatable. `fleet ls --tag NAME` "
                                               "finds it again"),
            kind: str = typer.Option(None, "--kind", help="permanent|rental|shared|appliance|mobile"),
            json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
            dry_run: bool = typer.Option(False, "--dry-run", help="probe and show what would be recorded; record nothing")):
    """Add a machine to the fleet, enrolling it.

    [dim]Example:[/dim]  fleet add "ssh -p 58418 root@1.2.3.4"
    """
    _first_run("add")
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
    if not this_machine:
        _split_a_clone(devices, dev, res)
    if dry_run:
        _emit({"device": dev.name, "id": dev.id, "kind": dev.kind.value,
               "status": res.status.value}, True)
        return
    tags = _tags(tag) or []

    def _record(current):
        # Into the inventory as it is now, not as it was before the probe: another agent
        # may have added or edited a machine in the seconds the probe took, and writing
        # back the list loaded then would silently undo that.
        current, action = inv.upsert(current, dev)
        # `upsert` matches on id, merges only endpoints and discards every other field of
        # the incoming record, so on a machine already known -- re-adding a rental whose
        # port moved, say -- the tags would be dropped without a word. Apply them to the
        # record that actually survived.
        if tags and (survivor := inv.find_exact(current, dev.id)):
            apply_edits(survivor, add_tags=tags)
        return current, action

    devices, action = inv.update(_record)
    # A machine already known keeps its own name: the record that survived is the one to
    # report. Reporting the requested name sent agents after a machine that did not exist
    # ("No device named 'loop'") on the very next command.
    asked = dev.name
    if action in ("endpoint_added", "unchanged") and (
            survivor := inv.find_exact(devices, dev.id)):
        dev.name = survivor.name
    for t in tags:
        if view_mod.is_fact_name(t):
            console.print(f"  [dim]note: {t!r} is also derived from telemetry — "
                          "kept, since a probe cannot see everything[/dim]")
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
        if action in ("endpoint_added", "unchanged") and name and name != dev.name:
            console.print(f"  [dim]it keeps its name; to rename it: [bold]fleet edit "
                          f"{dev.name} --name {asked}[/bold][/dim]")
        else:
            # A key refused on first contact is the normal start of enrolling, not a
            # fault: printed as "auth_failed: credentials rejected" it read as the add
            # having failed, one line before the key went in and it succeeded.
            enrolling = (res.status is Status.AUTH_FAILED and where == "center"
                         and not this_machine)
            console.print(f"[green]✓[/green] added [bold]{dev.name}[/bold] "
                          f"({dev.kind.value}"
                          + (")" if enrolling else f", {res.status.value})"))
            if enrolling:
                console.print("  [dim]it does not accept a key from here yet -- "
                              "installing one[/dim]")
        if not res.ok and not (res.status is Status.AUTH_FAILED and where == "center"
                               and not this_machine and action == "added"):
            console.print(f"  [yellow]{res.status.value}[/yellow]: {res.error_detail}")

    # --json still enrols. An agent uses --json, and a flag that quietly did half the
    # command would be the worst kind of difference -- but enrolment talks, and that talk
    # must not land in the middle of the document.
    with _chatter_to_stderr(json_out):
        outcome = _enrol_after_add(dev, res, where=where, this_machine=this_machine)
        if outcome == "enrolled":
            _settle_center_edge(dev.id)
    _emit({"action": action, "name": dev.name, "id": dev.id, "kind": dev.kind.value,
           "status": res.status.value, "enrolment": outcome}, json_out)
    if outcome == "failed":
        raise typer.Exit(1)


def _split_a_clone(devices, dev, res) -> None:
    """Keep two machines apart when they share a machine-id but are not the same host.

    Dedupe on machine-id is what makes one box reached two ways into one record. But VMs
    and containers cloned from one image share the id too, and on a real fleet the
    second clone was merged into the first as "one device, not two": unreachable by its
    own name, with two pinned keys claiming one device.

    Asked of the machines themselves, not of what was recorded: the known machine is
    measured again now, at its own address. If it answers as the same machine-id but
    another host -- another hostname, or booted at another time -- there are two of
    them. One box reached two ways is one host at one boot. Only when the known machine
    cannot be reached is its last reading used, and then only the hostname can tell.

    The clone gets an id of its own, and that id is written on it (see
    `identity.assigned_id`), so its own fleet and every later probe agree with the
    center about which machine it is -- rather than splitting it again on every add,
    or the clone believing it is the machine it was cloned from.
    """
    import secrets

    from .onboard import slugify

    existing = inv.find_exact(devices, dev.id)
    new = res.snapshot
    if existing is None or existing.id != dev.id or new is None:
        return
    new_eps = inv.endpoints_of(dev)
    old_eps = sorted(inv.endpoints_of(existing), key=lambda e: e.preference)
    if new_eps and any((e.target, e.port) == (new_eps[0].target, new_eps[0].port)
                       for e in old_eps):
        return                              # the same address: the same machine
    host = new.hostname or ""
    other, why = None, ""
    if old_eps:
        now = run_probe(old_eps[0], mode=existing.probe_mode, timeout=15)
        if now.snapshot is not None:
            if derive_id(now.snapshot, old_eps[0]) != dev.id:
                return                      # its old address is someone else now: moved
            other = now.snapshot
    if other is not None:
        booted = lambda s: (s.ts - s.uptime_s) if s.uptime_s is not None else None
        b_old, b_new = booted(other), booted(new)
        if other.hostname and host and other.hostname != host:
            why = f"calls itself {host!r}, not {other.hostname!r}"
        elif b_old is not None and b_new is not None and abs(b_old - b_new) > 120:
            why = "is running at the same time, booted at another moment"
        else:
            return                          # one host, one boot: one machine, two routes
    else:
        conn = store.connect()
        try:
            _, snap = store.latest(conn, existing.id)
        finally:
            conn.close()
        before = (snap or {}).get("hostname") or ""
        if not before or not host or before == host:
            return
        why = f"calls itself {host!r}, not {before!r}"

    taken_ids = {d.id for d in devices}
    suffix = slugify(host) if host and host != (other.hostname if other else "") else ""
    new_id = f"{dev.id}:{suffix}" if suffix else ""
    while not new_id or new_id in taken_ids:
        new_id = f"{dev.id}:{secrets.token_hex(3)}"
    dev.id = new_id
    if dev.name == existing.name:
        dev.name = inv_unique(devices, slugify(host) or existing.name)
    console.print(f"  [yellow]![/yellow] {dev.name} shares a machine-id with "
                  f"{existing.name} but {why} -- most likely cloned from the same image. "
                  "Recorded as a separate machine.")
    from .reconcile import _remote
    from .ssh.keys import remote_device_id_command

    plat = remote_platform({"uname_s": new.uname_s, "os": new.os})
    ok, said = _remote(new_eps[0], remote_device_id_command(new_id, platform=plat),
                       platform=plat, capture=True) if new_eps else (False, "")
    if not (ok and "ok" in said):
        console.print("  [dim]its id could not be written on it, so it may be taken for "
                      f"{existing.name} again; giving it a machine-id of its own fixes "
                      "that: `systemd-machine-id-setup` (after emptying /etc/machine-id) "
                      "on it[/dim]")


def inv_unique(devices, wanted: str) -> str:
    taken, name, n = inv.handles(devices), wanted, 2
    while name in taken:
        name, n = f"{wanted}-{n}", n + 1
    return name


def _duration_s(text: str) -> int:
    """`15m`, `2h`, `90s`, or plain minutes. 0 for anything unreadable."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", text or "")
    if not m:
        return 0
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}[m.group(2)]


def _listening(sync_url: str, limit_s: float = 5.0) -> bool | None:
    """Whether a listener answers where the invite will send the machine.

    Asked of the address itself, not of the service manager: a listener started by hand
    with `--listen` is not a service, and a service can report running while its port is
    closed -- either way the question the person needs answered is whether a join would
    get through.

    None when the answer did not come within `limit_s`. urlopen's timeout covers the
    connection, not the name lookup, and on a real macOS runner the center's own
    `.local` name took 35 seconds to resolve -- every `fleet invite` sat silent that long.
    """
    import threading
    import urllib.request

    from .ops.join import join_url

    health = join_url(sync_url)[: -len("/join")] + "/health"
    answer: list[bool] = []

    def ask() -> None:
        try:
            with urllib.request.urlopen(health, timeout=3) as resp:
                answer.append(resp.status == 200)
        except Exception:
            answer.append(False)

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(limit_s)
    return answer[0] if answer else None


@app.command("invite")
def cmd_invite(name: str = typer.Argument(None, help="what the machine will be called; "
                                                     "omit to let it say"),
               ttl: str = typer.Option("15m", "--ttl", metavar="TIME",
                                       help="how long it may be used for: 15m, 2h, 1d"),
               list_: bool = typer.Option(False, "--list", help="show recent invites"),
               withdraw: str = typer.Option(None, "--revoke", metavar="ID",
                                            help="withdraw an invite before it is used"),
               url_opt: str = typer.Option("", "--url", metavar="URL",
                                           help="the address the machine should dial; "
                                                "defaults to where the center listens"),
               json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
    """Let one machine join by itself: print a code to run there with `fleet join`.

    The other way in. `fleet add` has the center dial the machine; an invite has the
    machine dial the center, so no password is typed and a machine the center cannot
    reach can still join. Single use, and valid for minutes.

    [dim]Example:[/dim]  fleet invite gpu-box --ttl 30m
    """
    _first_run("invite")
    from . import links
    from .state import invites as invites_mod
    from .ops.join import encode_code

    try:
        acc = acl.load()
    except acl.AccessError:
        from .ops import member

        if _fleet_membership() != "member":
            _not_in_a_fleet(False)
            return
        err.print(f"[red]Only the center can invite a machine.[/red] Run this on "
                  f"[bold]{member.center_name()}[/bold].")
        raise typer.Exit(2)
    if not (list_ or withdraw):
        _refuse_while_handing_over(acc)
    if not acl.is_center(acc) or _stepped_down(acc):
        err.print(f"[red]Only the center can invite a machine.[/red] Run this on "
                  f"[bold]{acc.name_of(acc.center)}[/bold].")
        raise typer.Exit(2)

    if list_:
        rows = invites_mod.load()
        now = int(time.time())
        if _emit([{"id": i.id, "name": i.name, "state": i.state(now),
                   "expires_at": i.expires_at, "used_as": i.used_as,
                   "used_by": i.used_by} for i in rows], json_out):
            return
        if not rows:
            console.print("[dim]no invites in the last day[/dim]")
            return
        for i in rows:
            state = i.state(now)
            what = {"open": f"open, {max(0, i.expires_at - now) // 60}m left",
                    "used": f"used by {i.used_as or i.used_by[:20]}",
                    "expired": "expired", "revoked": "withdrawn"}[state]
            console.print(f"{i.id}  {i.name or '[dim](any name)[/dim]'}  {what}")
        return

    if withdraw:
        try:
            got = invites_mod.revoke(withdraw)
        except invites_mod.InviteError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        state = got.state()
        if state == "revoked":
            console.print(f"[green]✓[/green] invite {got.id} withdrawn")
        else:
            console.print(f"[dim]· invite {got.id} is already {state}; nothing to "
                          "withdraw[/dim]")
        return

    seconds = _duration_s(ttl)
    if seconds <= 0:
        err.print(f"[red]Not a duration: {ttl!r}[/red] -- try 15m, 2h or 1d")
        raise typer.Exit(2)
    if name:
        if name in inv.handles(inv.load()):
            err.print(f"[red]{name} is already the name of a machine here.[/red]")
            raise typer.Exit(2)
    invite, secret = invites_mod.create(name=name or "", ttl_s=seconds)
    # Where the listener said it answers, when it has run here; the computed default
    # only for a center that has never listened.
    url = url_opt or acl.center_url() or center_advertise_url(acc)
    code = encode_code(url, acc.center, invite.id, secret)
    serving = _listening(url)
    if _emit({"id": invite.id, "name": invite.name, "expires_at": invite.expires_at,
              "code": code, "command": f"fleet join {code}",
              "install": links.install_line(code),
              "install_windows": links.install_line_windows(code),
              "center_url": url, "listening": serving}, json_out):
        return
    minutes = max(1, seconds // 60)
    console.print(f"[green]✓[/green] invite {invite.id}"
                  + (f" for [bold]{invite.name}[/bold]" if invite.name else "")
                  + f" -- single use, valid {minutes} minute{'s' if minutes != 1 else ''}")
    console.print("  run this on the new machine:\n")
    # print, not console.print: rich would wrap a long code across lines, and a code
    # that does not survive copy-paste is no code at all.
    print(f"    fleet join {code}\n")
    # The machine may not have fleet yet. One line installs it *as a member*, so it
    # never starts a fleet of its own first.
    console.print("  [dim]no fleet there yet? this installs it and joins in one go:[/dim]\n")
    print(f"    {links.install_line(code)}\n")
    console.print("  [dim]Windows (PowerShell):[/dim]\n")
    print(f"    {links.install_line_windows(code)}\n")
    console.print(f"  [dim]it will dial {url}; the center reaches it back over ssh, "
                  "so sshd must be running there[/dim]")
    if serving is None:
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname or url
        console.print(f"[yellow]![/yellow] {host} did not even resolve here within 5s, so "
                      "the machine may not reach it either -- if it cannot, give it an "
                      "address it can: [bold]fleet invite --url http://ADDRESS:7373/sync"
                      "[/bold]")
    elif not serving:
        console.print("[yellow]![/yellow] the center is not listening, so nothing can "
                      "join yet -- start it with [bold]fleet service install[/bold] or "
                      "[bold]fleet center --listen[/bold]")


@app.command("join")
def cmd_join(code: str = typer.Argument(..., help="the code `fleet invite` printed"),
             name: str = typer.Option(None, "--name",
                                      help="what to call this machine, if the invite "
                                           "did not say"),
             ssh_command: str = typer.Option(None, "--ssh", metavar="CMD",
                                             help='how the center reaches this machine, '
                                                  'e.g. "ssh -p 2222 me@10.0.0.5"; '
                                                  "defaults to the address it sees"),
             json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
    """Join a fleet with an invite from its center. No password, nothing to approve.

    [dim]Example:[/dim]  fleet join fleet1:eyJ1Ijoi...
    """
    from .ops.join import join as _join

    try:
        summary = _join(code, name=name or "",
                        ssh_command=ssh_command or "")
    except FleetError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(exc.code)
    if _emit(summary, json_out):
        if not summary["center_key_installed"]:
            raise typer.Exit(1)
        return
    console.print(f"[green]✓[/green] joined fleet {summary['fleet_id']} as "
                  f"[bold]{summary['name']}[/bold] -- {summary['machines']} machine(s) known")
    if summary["reached_as"]:
        console.print(f"  [dim]the center will reach this machine as "
                      f"{summary['reached_as']}[/dim]")
    if summary["center_key_installed"]:
        console.print("  [dim]the center's key is in this machine's authorized_keys; "
                      "it keeps itself current from now on[/dim]")
    else:
        err.print("[yellow]Joined, but the center's key could not be installed here,[/yellow] "
                  "so the center cannot manage this machine yet:")
        err.print(f"  [dim]{escape(summary['install_output'])}[/dim]")
        err.print("  [dim]run [bold]fleet join[/bold] again with the same code to retry[/dim]")
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
             role: str = typer.Option(None, "--role", help="a label for what the machine is for; the center moves only with `fleet center NAME`"),
             cost: float = typer.Option(None, "--cost", metavar="USD",
                                        help="what it costs per hour, for the $/HR column "
                                             "and the fleet's burn rate; 0 clears it"),
             json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
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
    old_name, dev_id = dev.name, dev.id

    def _edit(target, current):
        return apply_edits(target, name=new_name, alias=new_alias,
                           add_tags=_tags(tag), drop_tags=_tags(untag),
                           taken=inv.handles(current, excluding=target.id),
                           endpoint=endpoint, disk_paths=paths,
                           role=None if role == "center" else role,
                           usd_per_hour=cost)

    try:
        # A dry run on the copy loaded above, to refuse or report "nothing to change"
        # before taking the lock.
        result = _edit(dev, devices)
    except ValueError as exc:
        # apply_edits refuses in words -- a clashing alias, a name already taken -- and
        # those words are the whole answer. A traceback around them was not.
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if role == "center":
        # Flipping the role alone strands the fleet: members verify the list against the
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

    def _apply(current):
        # Again, on the inventory as it is now: two agents tagging one machine at once
        # each loaded the same list, and whichever saved last erased the other's tag.
        target = inv.find_exact(current, dev_id)
        if target is None:
            raise ValueError(f"{old_name} was removed meanwhile")
        return current, (target, _edit(target, current))

    try:
        _, (dev, result) = inv.update(_apply)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if dev.name != old_name:
        _rename_in_access_list(dev, old_name)
    if endpoint is not None:
        _route_known(dev)
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
        console.print(f"  [dim]run `fleet ls {dev.name} -r` to confirm it answers.[/dim]")


def _route_known(dev) -> None:
    """A machine marked as having no route has one now: let the center manage it again."""
    try:
        acc = acl.load()
    except acl.AccessError:
        return
    if not acl.is_center(acc):
        return

    def restore(current):
        return any([meta.pop("no_route", None) for meta in current.keys.values()
                    if meta.get("device_id") == dev.id])

    _, changed = acl.update(restore)
    if changed:
        console.print("  [dim]the center will manage it again from its next sweep[/dim]")


def _rename_in_access_list(dev, old_name: str) -> None:
    """Carry a rename into the access list, when this machine holds it.

    The list names each key, and `fleet access NAME` resolves by that name. Renaming only
    the inventory left the two disagreeing: the sweep reported the new name, `fleet
    access` the old one, and a grant to the new name failed with "no machine called".
    Off the center there is no list to update -- `_access_fp` finds the machine by id
    there and here alike, so the old name in the list no longer matters to anyone.
    """
    try:
        acc = acl.load()
    except acl.AccessError:
        return
    if not acl.is_center(acc):
        return

    def rename(current):
        for meta in current.keys.values():
            if (dev.id and meta.get("device_id") == dev.id) or meta.get("name") == old_name:
                meta["name"] = dev.name

    acl.update(rename)


def configured_repo() -> str:
    """Where a device should clone fleet from.

    Falls back to an origin we can find, so the common case -- deploying the repo you
    are standing in -- needs no configuration at all.

    Two places, because there are two ways to be running fleet. From a source checkout,
    the package sits inside the repo. From an install, it sits in uv's tool directory
    with no git anywhere near it -- but the clone `fleet install` made is still on disk,
    and its origin is by definition the repo that machine was deployed from. Without the
    second look, every machine fleet had itself installed answered "No repo to update
    from" and could update nothing without being told a URL it already knew.
    """
    configured = load_config().get("repo")
    if configured:
        return str(configured)
    roots = [Path(__file__).resolve().parent.parent.parent]
    if source := install_source():
        roots.append(source)
    roots.append(Path.home() / ".local" / "share" / "fleet")
    for root in roots:
        try:
            out = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    # Installed from PyPI or by the one-line installer: no clone anywhere, and the
    # repository it was published from is the one to deploy.
    from . import links

    return f"{links.REPO}.git"


def _remote_platform_of(dev) -> str:
    """Which shell this device speaks, from the last probe.

    An unprobed machine reads POSIX, which is the safe way to be wrong: a POSIX script
    on Windows fails loudly, where the reverse can appear to succeed.
    """
    conn = store.connect()
    try:
        _, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    return remote_platform(snap)


def run_installer(ep, script: str, *, forward_agent: bool = True,
                  platform: str = "") -> tuple[int, str]:
    argv = build_install_argv(ep, forward_agent=forward_agent, platform=platform)
    p = sshrun(argv, input=payload_for(script, platform), timeout=900)
    return p.returncode, (p.stdout + p.stderr).decode(errors="replace")


@app.command("install")
def cmd_install(name: str = typer.Argument(None,
                                          help="the machine to install fleet on"),
                repo: str = typer.Option(None, "--repo", metavar="URL",
                                         help="git URL to clone; defaults to config or this checkout"),
                ref: str = typer.Option("main", "--ref", help="branch or tag to install"),
                role: str = typer.Option(None, "--role", hidden=True,
                                         help="kept for old scripts; only `none` is accepted"),
                forward_agent: bool = typer.Option(True, "--forward-agent/--no-forward-agent",
                                                   help="authenticate the clone as you, "
                                                        "leaving no credential on the device")):
    """Install fleet on a device, so you and its agents can use the fleet from there.

    A machine only reached *by* fleet needs nothing installed; this is for one you also
    work on. It installs from git; re-running updates an existing install.

    [dim]Example:[/dim]  fleet install machine_A
    """
    if role == "center":
        # Checked before anything reaches the network. It used to be validated after the
        # install had already run, so an invalid flag cost a full ssh timeout before
        # being told it was invalid.
        err.print("[red]Use [bold]fleet center NAME[/bold] to move the role.[/red]")
        raise typer.Exit(2)
    if role not in (None, "none"):
        # `backup` and anything else used to be recorded and then meant nothing.
        err.print(f"[red]There is no {role!r} role.[/red] [dim]A machine is the center or "
                  "it is not; move the role with fleet center NAME.[/dim]")
        raise typer.Exit(2)
    if name is None:
        # This machine is never an ssh target -- its record has no address -- so
        # "install here" could only fail with "no endpoint recorded".
        err.print("[red]Name the machine to install fleet on.[/red] [dim]This one already "
                  "runs fleet: [bold]fleet update[/bold] updates it.[/dim]")
        raise typer.Exit(2)
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

    platform = _remote_platform_of(dev)

    console.print(f"[dim]installing fleet on {dev.name} from {url} ({ref})[/dim]")
    code, output = run_installer(sorted(eps, key=lambda e: e.preference)[0],
                                 install_script(url, ref=ref, platform=platform,
                                                from_git=bool(repo) or ref != "main"),
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
        def _set_role(current):
            if target := inv.find_exact(current, dev.id):
                target.role = role
                inv.touch(target)
            return current, None

        inv.update(_set_role)
        dev.role = role
    # The role only when it is one: "gpu is now none" read as a sentence about the
    # machine rather than about a field nobody had asked to change.
    what = output.strip().splitlines()[-1] if output.strip() else "installed"
    console.print(f"[green]✓[/green] {dev.name}: {what}"
                  + (f" — role [bold]{role}[/bold]" if role and role != "none" else ""))
    _hand_the_fleet_to(dev)


def _hand_the_fleet_to(dev) -> None:
    """Make a machine that just got fleet a member now, not at the next sweep.

    Found on a real fleet: `fleet install gpu` succeeded, and on gpu every command then
    said "not in a fleet" -- membership arrives in the signed copy a sweep hands over,
    and the install was the moment fleet was first there to receive one. The center is
    connected already; one more envelope is cheaper than a confused hour.
    """
    try:
        acc = acl.load()
    except acl.AccessError:
        return
    if not acl.is_center(acc):
        return
    eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
    if not eps:
        return
    with contextlib.suppress(Exception):
        code, _ = _sync.run_sync(eps[0], inv.dumps(inv.load()))
        if code == 0:
            console.print(f"  [dim]{dev.name} has the fleet now and keeps itself "
                          "current[/dim]")




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
                                               help="every device that already runs fleet"),
               repo: str = typer.Option(None, "--repo", metavar="URL", help="git URL to deploy from; defaults to config or this checkout"),
               ref: str = typer.Option("main", "--ref", help="branch or tag")):
    """Update fleet on the machines that have it, the way it was installed there.

    A machine with fleet from PyPI gets `uv tool upgrade` (or `pipx upgrade`), one with
    fleet's own checkout is updated from git, and your own source install is left as it
    is. `--repo` or `--ref` asks for git everywhere. Updates this machine without ssh,
    and `--all` every machine that runs fleet.

    A device with no fleet is skipped and named, never given one: most of a fleet is
    meant to have nothing installed, and `fleet install NAME` is how you change that on
    purpose.

    [dim]Example:[/dim]  fleet update --all
    """
    devices = inv.load()
    url = repo or configured_repo()
    # Asking for a repo or a ref by name is asking for git. Otherwise each machine is
    # updated the way fleet was installed there (install.py, _how_installed_posix).
    from_git = bool(repo) or ref != "main"
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
    ok = failed = skipped = 0

    # This machine first and without ssh. The center is never an ssh target, so
    # connecting to ourselves would fail on exactly the machine most likely to be
    # running the command.
    here = not name or (targets and any(d.id == me for d in targets))
    # On Windows this machine goes last, in the background, once this process has
    # exited: a running fleet holds the very files the update replaces.
    here_later = here and local_platform() == WINDOWS
    if here and not here_later:
        console.print(f"[dim]updating this machine from {url} ({ref})[/dim]")
        # local_platform, not `sh -c`: on a Windows center that shell is git's, and the
        # POSIX script half-runs under it. This is the machine running the command, so
        # it certainly has fleet -- update_only would be true either way, and saying so
        # keeps one script shape for every path.
        local_script = install_script(url, ref=ref, platform=local_platform(),
                                      update_only=True, from_git=from_git)
        p = subprocess.run(local_install_argv(),
                           input=payload_for(local_script, local_platform()),
                           capture_output=True)
        if p.returncode == 0:
            console.print("[green]✓[/green] this machine")
            ok += 1
        elif p.returncode == NOTHING_TO_UPDATE:
            # Running from a source checkout with nothing installed. The same answer as
            # for any other machine, rather than a red ✗ for the one you are sitting at.
            console.print("[dim]·[/dim] this machine has no installed fleet "
                          "[dim]-- you are running it from a checkout[/dim]")
            skipped += 1
        else:
            said = (p.stderr or p.stdout or b"")
            err.print(f"[red]✗[/red] this machine\n"
                      f"{said.decode(errors='replace')[-400:]}")
            failed += 1
    targets = [d for d in targets if d.id != me]

    for dev in targets:
        eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
        # Per device, because a fleet is not one platform. Building the script once and
        # sending it to everything handed the POSIX installer to a Windows center, which
        # runs it under git's sh.exe far enough to break the installation it was meant
        # to update -- `fleet install` had always read the platform, and this had not.
        platform = _remote_platform_of(dev)
        console.print(f"[dim]updating {dev.name}[/dim]")
        code, output = run_installer(eps[0],
                                     install_script(url, ref=ref, platform=platform,
                                                    update_only=True, from_git=from_git),
                                     platform=platform)
        if code == 0:
            console.print(f"[green]✓[/green] {dev.name}")
            ok += 1
        elif code == NOTHING_TO_UPDATE:
            # Not a failure, and not something to fix: most of a fleet is meant to have
            # nothing installed. Named rather than counted silently, so `--all` still
            # accounts for every machine it touched.
            console.print(f"[dim]·[/dim] {dev.name} [dim]has no fleet -- "
                          f"install one with [bold]fleet install {dev.name}[/bold][/dim]")
            skipped += 1
        else:
            # One unreachable device must not stop the rest: a fleet half-updated on
            # purpose is better than a fleet half-updated by an exception.
            err.print(f"[red]✗[/red] {dev.name} (exit {code}) "
                      f"[dim]{output.strip()[-120:]}[/dim]")
            failed += 1

    if here_later:
        from .install import update_windows_in_background

        log, started = update_windows_in_background(
            install_script(url, ref=ref, platform=local_platform(), update_only=True,
                           from_git=from_git))
        if started:
            console.print(f"[green]✓[/green] this machine: updating in the background, "
                          "once this command has exited [dim](Windows cannot replace a "
                          f"program while it runs). It takes a minute; the log is {log}, "
                          "and [bold]fleet --version[/bold] shows the result.[/dim]")
            ok += 1
        else:
            err.print("[red]✗[/red] this machine: the background update did not start "
                      f"[dim]-- what PowerShell said is in "
                      f"{log.with_name('update-run.log')}[/dim]")
            failed += 1

    if ok + failed + skipped > 1 or failed:
        tail = f", {skipped} skipped" if skipped else ""
        console.print(f"\n[dim]{ok} updated, {failed} failed{tail}[/dim]")
    if failed:
        raise typer.Exit(1)


@app.command("sync")
def cmd_sync(serve: bool = typer.Option(False, "--serve",
                                        help="run on the center: merge stdin, print the result"),
             from_url: str = typer.Option(None, "--from", metavar="URL",
                                          help="dial a center at this address and "
                                               "remember it, when it cannot reach you"),
             json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
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
        # this filter runs on a member that every granted peer holds a key for. Verifying
        # the access list and taking the routing on trust would have secured the policy
        # and left the routes open, so the whole envelope is checked.
        pinned = acl.trusted_center_pubkey()
        relayed = []
        url = ""
        sent_at = 0
        try:
            if pinned:
                note, signer = acl.unseal_trusting(raw, pinned)
                if signer != pinned:
                    acl.pin_center_pubkey(signer)     # walked a signed handover to it
                body, relayed, url = note["inventory"], note["telemetry"], note["center_url"]
                sent_at = note.get("sent_at", 0)
            else:
                body = acl.unseal_first_contact(raw)
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        acl.note_center_seen()
        clock.note_center_time(sent_at)
        # Learned here, over a payload already signed by the key this machine pinned at
        # enrolment. After this it can refresh itself and stop waiting to be swept.
        acl.note_center_url(url)
        if pinned:
            acl.note_fleet_id(note.get("fleet_id", ""))
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
        # Authoritative: this is the center's list, verified above against the key this
        # machine pinned, so a route the center removed is removed here too. Unioned
        # instead, a member kept every route it had ever held, under the center's
        # timestamp -- and later authoritative pulls, tied on that timestamp, never
        # took them away.
        merged, changes = inv.update(
            lambda current: inv.merge_from_center(current, incoming, sent_at=sent_at))
        sys.stdout.write(inv.dumps(merged))
        return

    devices = inv.load()

    # Whether this machine is the center is settled by the access list -- possession of
    # the signing key -- not by `Device.role`. role rides `inventory.merge`, where a peer
    # with a fast clock could flip it, and comparing `identity.local_device_id()` adds a third way
    # to be wrong: it shells out to `ioreg` on macOS, which is not on cron's PATH, and
    # returns nothing at all on Windows. Ask the one authority.
    try:
        acc_here = acl.load()
        if acl.is_center(acc_here) and not _stepped_down(acc_here):
            with _as_exit(), _chatter_to_stderr(json_out):
                _sweep(devices)
            _emit({"synced": "center", "machines": len(inv.live(inv.load()))}, json_out)
            return
    except acl.AccessError:
        pass                               # no access list here: a member, or no fleet yet

    if acl.center_url() and acl.trusted_center_pubkey():
        # A member that knows where the center listens asks it, as every read already
        # does. Dialling the center over ssh, below, needs an endpoint the center's own
        # record never has -- so on a joined machine `fleet sync` could only fail.
        before = acl.center_last_seen()
        _sync.ensure_fresh(force=True)
        if acl.center_last_seen() > before:
            n = len(inv.live(inv.load()))
            if _emit({"synced": "member", "machines": n}, json_out):
                return
            console.print(f"[green]✓[/green] up to date with the center ({n} machines)")
            return
        err.print(f"[yellow]The center did not answer at {acl.center_url()}.[/yellow] "
                  "Everything here keeps working from the last copy.")
        raise typer.Exit(1)

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

    Anything unreachable backs off instead of being redialled every couple of seconds.
    Shared multi-user hosts are probed only when asked for by name (`fleet ls NAME`).

    [dim]Example:[/dim]  fleet top machine_A -i 1
    """
    _first_run("top")
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
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        console.print(frame())
        if sys.stdout.isatty():
            # A terminal that cannot move the cursor: the live view drew nothing at all
            # and said nothing about why, so one frame and the reason instead.
            console.print("[dim]this terminal (TERM=dumb) cannot redraw in place, so "
                          "this is one frame -- set TERM, e.g. xterm-256color, for the "
                          "live view[/dim]")
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
    leave, or the user's shell is left unusable.

    Not on Windows, which has no termios (`fleet top` died there on "No module named
    'termios'"): its console hands single keys to msvcrt without any mode change.
    """
    if not sys.stdin.isatty() or sys.platform == "win32":
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
    if sys.platform == "win32":
        # select() takes only sockets on Windows; the console is polled instead.
        import msvcrt

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return msvcrt.getwch()
            time.sleep(0.05)
        return None
    if _select.select([sys.stdin], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return None


@app.command("rm")
def cmd_rm(name: str = typer.Argument(..., help="the exact name; a prefix is never enough"),
           yes: bool = typer.Option(False, "--yes", "-y", help="do not ask for confirmation")):
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
            centre = acl.is_center(acl.load())
        except acl.AccessError:
            # No access list. On a member that is the normal state, not "no fleet yet":
            # treating it as a free-for-all let a member tombstone any machine, and the
            # tombstone then rode the merge to the center and deleted it fleet-wide.
            centre = _fleet_membership() == ""
        if not centre:
            err.print(f"[red]Only the center can remove {dev.name}.[/red]")
            err.print("  [dim]a machine can remove itself -- that is leaving -- but "
                      "removing another revokes its keys, which only the center "
                      "can do[/dim]")
            raise typer.Exit(2)

    with contextlib.suppress(acl.AccessError):
        if not itself:
            _refuse_while_handing_over(acl.load())
    if not yes and not typer.confirm(f"Remove {dev.name} ({dev.kind.value})?"):
        raise typer.Exit(1)

    # Revoke before forgetting, while the record still says where it is: its key on
    # every other machine, and every fleet key on it. Then drop the pins.
    unreached: list[str] = []
    try:
        acc = acl.load()
        if acl.is_center(acc) and not itself:
            # Out of the list first, then off the machines, working from the list as it
            # was. The other way round, a retry running meanwhile -- the listener's --
            # still saw the pins, still wanted the center's key on the machine being
            # removed, and put it back after it had been taken off.
            def forget_keys(current):
                before = copy.deepcopy(current)
                for fp in [fp for fp, m in current.keys.items()
                           if m.get("device_id") == dev.id]:
                    current.allow = [e for e in current.allow if fp not in (e.src, e.dst)]
                    current.keys.pop(fp, None)
                return before

            _, before = acl.update(forget_keys)
            _, unreached = _remove_now(before, dev)
    except acl.AccessError:
        pass

    def _forget(current):
        inv.remove(current, inv.find_exact(current, dev.id))
        return current, None

    inv.update(_forget)
    console.print(f"[green]✓[/green] removed {dev.name}")
    if unreached:
        console.print("  [yellow]not everything could be reached:[/yellow]")
        for line in unreached:
            console.print(f"    {line}")
        if any(line.startswith(f"{dev.name}:") for line in unreached):
            console.print(f"  [dim]{dev.name} keeps this fleet's keys until removed by "
                          "hand -- on it, delete the `# fleet:` blocks from "
                          "~/.ssh/authorized_keys, or run [bold]fleet center --leave"
                          "[/bold] there if it has fleet[/dim]")


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
def cmd_ssh(ctx: typer.Context,
            name: str = typer.Argument(..., help="a machine's name or alias")):
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
    # A cached refusal is evidence only while it is fresh. A grant applies on the target
    # within seconds of `fleet access --allow` on the center, and nothing tells this
    # machine that it happened, so an old reading refused a connection that now works --
    # and sent the reader to `fleet sync`, which a member holding no key on the center
    # cannot run either. Stale, we try: a key that really is missing fails in under a
    # second with ssh's own message, which is a cheaper way to be wrong than this was.
    fresh_s = int(load_config().get("telemetry_ttl_s") or 0)
    probed_at = int((cached or {}).get("last_probe_at") or 0)
    if auth_of(dev, cached) == "needs_key" and time.time() - probed_at <= fresh_s:
        err.print(f"[yellow]{dev.name} rejected our key.[/yellow] Only the center can "
                  "install one:")
        err.print(f"  [bold]fleet add \"ssh ...\"[/bold] on the center, or "
                  "[bold]fleet sync[/bold] if it is already recorded")
        raise typer.Exit(2)
    # accept-new, as the probe has always used: an unknown host key is recorded, a
    # changed one is still refused. Without it the first `fleet ssh` from a member to a
    # machine it had just been granted failed "Host key verification failed" -- ssh asks
    # a question there is no terminal to answer when an agent is the one asking, so a
    # grant that had landed was unusable by exactly the caller it was made for.
    # Keys only. fleet never handles a password, and falling back to one meant a
    # machine you hold no grant on answered "Permission denied, please try again" --
    # a password prompt, twice, which reads as though one would work. Without a grant
    # the answer is simply no, and ssh says so at once.
    argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]
    # The fleet key, or `fleet ssh` connects with a personal key that fleet no longer
    # installs anywhere -- and this is the most-used command in the tool.
    if _cfg.FLEET_KEY.exists():
        argv += ["-i", str(_cfg.FLEET_KEY)]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.identity:
        argv += ["-i", ep.identity]
    if ep.jump:
        argv += ["-J", ep.jump]
    argv += ["--", f"{ep.user}@{ep.target}" if ep.user else ep.target]
    extra = [a for a in ctx.args if a != "--"]
    if extra:
        argv.append(remote_command(extra, windows=platform == "windows"))

    if sys.platform == "win32":
        # Windows has no exec: os.execvp starts ssh and exits this process at once with
        # 0, so every `fleet ssh NAME -- cmd` reported success whatever cmd did (found on
        # a real Windows runner), and an interactive shell fought the prompt for input.
        # Ctrl+C belongs to ssh and the remote command. Reaching this process too, it
        # made subprocess kill ssh -- dropping the session to stop one remote command.
        import signal

        before = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            code = subprocess.call(argv)
        finally:
            signal.signal(signal.SIGINT, before)
        raise typer.Exit(code)
    os.execvp("ssh", argv)      # replace this process; ssh owns the tty from here


def _access_fp(acc, token: str) -> str:
    """The fingerprint for a machine, from any handle you would type for it.

    The list's own names first, exactly as before. Then the inventory -- current name or
    alias -- matched on device id, because the list keeps the name a machine had when it
    was pinned and a machine can be renamed since. Exact only, as `acl.resolve` is: this
    picks who can reach what, and a prefix is not good enough for that.
    """
    try:
        return acl.resolve(acc, _canonical(token))
    except acl.AccessError as first:
        dev = inv.find_exact(inv.load(), token)
        if dev is not None:
            hits = [fp for fp, m in acc.keys.items() if m.get("device_id") == dev.id]
            if len(hits) == 1:
                return hits[0]
        raise first


@app.command("access")
def cmd_access(target: str = typer.Argument(None, help="one machine, instead of all"),
               allow: str = typer.Option(None, "--allow", metavar="MACHINE",
                                         help="let MACHINE reach the target"),
               deny: str = typer.Option(None, "--deny", metavar="MACHINE",
                                        help="stop MACHINE reaching the target"),
               user: str = typer.Option(None, "--user",
                                        help="whose authorized_keys; a grant defaults to the "
                                             "account the target is reached as, a revoke "
                                             "to every account"),
               migrate: bool = typer.Option(False, "--migrate",
                                            help="spend passwords an older fleet stored"),
               json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
    """Who may reach what, and change it.

    Granting installs a key; revoking removes one. Both are things the center does to a
    machine over ssh, so both can be pending -- and a revoke that has not reached its
    target is reported as not in effect, never as done.

    [dim]Example:[/dim]  fleet access machine_A --allow machine_B
    """
    _first_run("access")

    if migrate:
        with _as_exit():
            _migrate_passwords()
        return

    try:
        current = acl.load()
    except acl.AccessError:
        from .ops import member

        if _fleet_membership() != "member":
            _not_in_a_fleet(json_out)
            return
        center = member.center_name()
        if allow or deny:
            # Refused and named, never queued. A request filed here used to land in an
            # outbox that nothing ever read -- reported as "filed", applied never.
            err.print(f"[red]Only the center can change who may reach what.[/red] On "
                      f"[bold]{center}[/bold]:")
            err.print(f"  fleet access {target or 'NAME'} "
                      f"{'--allow' if allow else '--deny'} {allow or deny}"
                      + (f" --user {user}" if user != "root" else ""))
            raise typer.Exit(2)
        if _emit({"is_center": False, "center": center, "edges": None}, json_out):
            return
        console.print(f"Who may reach what is decided on [bold]{center}[/bold]; run "
                      "[bold]fleet access[/bold] there to see it.")
        console.print("  [dim]from here, [bold]fleet ssh NAME[/bold] working is the "
                      "answer for any one machine[/dim]")
        return

    if allow or deny:
        _refuse_while_handing_over(current)
    if (allow or deny) and _stepped_down(current):
        err.print("[red]Only the center can change who may reach what.[/red] Run it on "
                  "the new center.")
        raise typer.Exit(2)
    centre = acl.is_center(current)
    if (allow or deny) and not centre:
        err.print(f"[red]Only the center can change who may reach what.[/red] Run it on "
                  f"[bold]{current.name_of(current.center)}[/bold].")
        raise typer.Exit(2)

    if allow or deny:
        def change(acc):
            # Resolved and applied to the list as it is now, in turn with every other
            # writer: two agents granting at once each keep their grant.
            dst = _access_fp(acc, target)
            src = _access_fp(acc, allow or deny)
            if allow:
                # The account the center itself reaches the target as -- the one whose
                # authorized_keys it can write. Defaulting to root left every grant on a
                # machine added as ubuntu@ pending for good, and the grantee's
                # `fleet ssh` logs in as that account anyway.
                who = [user or (acc.keys.get(dst) or {}).get("user") or "root"]
                done = acl.grant(acc, src, dst, user=who[0])
            else:
                # "Stop the laptop reaching the NAS" means as anyone, unless one is named.
                who = [user] if user else sorted(
                    {e.user for e in acc.allow if e.src == src and e.dst == dst}) or ["root"]
                done = any([acl.revoke(acc, src, dst, user=w) for w in who])
            return src, dst, done, who

        try:
            current, (src, dst, changed, users) = acl.update(change)
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        verb = "granted" if allow else "revoked"
        # With --json, stdout is one document and nothing else: the progress lines go to
        # stderr. An MCP client parses stdout, and text before the JSON made every grant
        # it asked for come back unreadable.
        with _chatter_to_stderr(json_out):
            console.print(f"[green]✓[/green] {verb} {current.name_of(src)} -> "
                          f"{current.name_of(dst)} as {', '.join(users)}"
                          + ("" if changed else "  [dim](already so)[/dim]"))
            if changed:
                # Applied here rather than left for a sweep. You have just said what you
                # want, so telling you to run a second command to mean it was always a
                # poor trade -- and for a revoke it is worse than that: a machine that
                # waits to be asked would keep the key until it next happened to sync,
                # which for an idle machine is never, while the peer losing access
                # carries on using it.
                for who in users:
                    _apply_now(current, src, dst, who, install=bool(allow))
        change_done = {"change": verb, "from": current.name_of(src),
                       "to": current.name_of(dst), "user": ", ".join(users),
                       "changed": changed}

    ledger = rec.load_ledger()
    rows = []
    for edge in sorted(current.edges()):
        src, dst, who = edge
        if target and dst != _access_fp(current, target):
            continue
        st = ledger.get(">".join(edge), rec.EdgeState())
        rows.append({"from": current.name_of(src), "to": current.name_of(dst),
                     "user": who, "state": st.observed,
                     "pending_s": (int(time.time()) - st.pending_since)
                                  if not st.converged and st.pending_since else 0,
                     "last_error": st.last_error})
    # A revoke that has not reached its machine is still a key in that machine's
    # authorized_keys. Listing only what the list wants made it vanish from the table the
    # moment it was asked for -- exactly the "reported as done while the key is still
    # there" the access design rules out.
    wanted = set(current.edges())
    for key, st in sorted(ledger.items()):
        parts = tuple(key.split(">"))
        if len(parts) != 3 or parts in wanted or st.desired != "absent" or st.converged:
            continue
        src, dst, who = parts
        if target and dst != _access_fp(current, target):
            continue
        rows.append({"from": current.name_of(src), "to": current.name_of(dst),
                     "user": who, "state": "revoking",
                     "pending_s": (int(time.time()) - st.pending_since)
                                  if st.pending_since else 0,
                     "last_error": st.last_error or "revoke not applied yet -- the key is still there"})
    if _emit({"center": current.name_of(current.center), "edges": rows,
              **(change_done if allow or deny else {})}, json_out):
        return
    if not rows:
        console.print("[dim]no access granted yet[/dim]")
        return
    t = Table(box=None, pad_edge=False, header_style="bold")
    for col in ("", "FROM", "TO", "USER", "STATE", "NOTE"):
        t.add_column(col, no_wrap=(col != "NOTE"))
    for r in rows:
        live = r["state"] == "present"
        dot = ("[green]●[/green]" if live else
               "[red]○[/red]" if r["state"] == "revoking" else "[yellow]○[/yellow]")
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
               cancel: bool = typer.Option(False, "--cancel",
                                           help="keep the role after handing it over, "
                                                "if the successor never accepted"),
               receive: bool = typer.Option(False, "--receive", hidden=True,
                                            help="store a handover the center delivers "
                                                 "on stdin; run by the center over ssh"),
               json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
               force: bool = typer.Option(False, "--force", help="with --dissolve: do not ask, and finish even if some machines cannot be reached")):
    """Who decides, and handing that over.

    Only the current center can name the next one. No machine may promote itself, so an
    unplanned loss of the center means re-configuring by hand -- which is the price of
    there being exactly one machine that can open every door.

    [dim]Example:[/dim]  fleet center machine_B
    """

    if receive:
        from .ops.handover import receive as _receive
        try:
            fid = _receive(sys.stdin.read())
        except FleetError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(exc.code)
        console.print(f"handover of fleet {fid} stored; run fleet center --accept here")
        return

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
        # Remembered, so `fleet invite` -- another process, run later -- hands out the
        # address this listener actually answers on rather than recomputing a default
        # that is wrong the moment --port or --advertise was given.
        acl.note_center_url(url)
        console.print(f"[green]✓[/green] serving fleet {acc.fleet_id} on port {where}")
        console.print(f"  [dim]machines are told to dial {url}[/dim]")
        console.print("  [dim]only keys this fleet has pinned are answered; a new "
                      "machine gets in by enrolment or with [bold]fleet invite[/bold][/dim]")
        retry_s = int(load_config().get("access_retry_s") or 0)
        if retry_s > 0:
            # Grants and revokes the center could not apply when they were made -- the
            # machine was off -- are tried again from here, so a revoke does not wait
            # for somebody to remember `fleet sync`.
            import threading
            from .ops.sweep import retry_forever

            threading.Thread(target=retry_forever, args=(retry_s,), daemon=True,
                             name="fleet-access-retry").start()
            console.print(f"  [dim]pending grants and revokes are retried every "
                          f"{retry_s // 60 or 1} min or so[/dim]")
        try:
            serve_center(port=where, advertise=url)
        except KeyboardInterrupt:
            console.print("\n[dim]stopped[/dim]")
        return

    if init:
        from .ops.firstrun import init_center
        try:
            acc, dev = init_center()
        except acl.AccessError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
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

    if not (leave or dissolve or accept or export or name or cancel):
        _first_run("center")               # a bare status check, on a machine in no fleet
    try:
        acc = acl.load()
    except acl.AccessError:
        _center_off_the_center(leave=leave, dissolve=dissolve, accept=accept,
                               export=export, name=name, json_out=json_out)
        return
    if _stepped_down(acc):
        _center_off_the_center(leave=leave, dissolve=dissolve, accept=accept,
                               export=export, name=name, json_out=json_out)
        return

    if cancel:
        if not acl.HANDING_PATH.exists():
            console.print("[dim]· no handover is outstanding[/dim]")
            return
        acl.HANDING_PATH.unlink()
        console.print("[green]✓[/green] this machine keeps the role; the handover is "
                      "cancelled here")
        console.print("  [dim]the successor still holds the record you signed and could "
                      "accept with it; to make sure it cannot manage the fleet, take its "
                      "grants away with [bold]fleet access[/bold][/dim]")
        return
    if dissolve or name:
        _refuse_while_handing_over(acc)
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
    # `role` names this machine's install mode in one word -- center or member -- where
    # the older keys need reading together.
    if _emit({"role": "center" if centre else "member",
              "center": acc.name_of(acc.center), "is_center": centre,
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

        state = service.status(DEFAULT_PORT)
        if state == "running":
            console.print(f"serving  {center_advertise_url(acc)}")
        elif state == "installed":
            console.print("[yellow]![/yellow] the service is installed but not running "
                          "-- machines cannot refresh themselves")
        elif state == service.UNAVAILABLE:
            console.print("[yellow]![/yellow] not serving, and there is no service "
                          f"manager here: {service.MANUAL}")
        else:
            console.print("[yellow]![/yellow] not serving; machines wait to be swept "
                          "-- [bold]fleet service install[/bold]")
    if note := staleness_note():
        console.print(f"[yellow]![/yellow] {note}")
    if not centre:
        console.print("\n[dim]Changes are made on the center. Losing it means "
                      "re-configuring by hand -- keep a copy: [bold]fleet center "
                      "--export[/bold][/dim]")












def _stepped_down(acc) -> bool:
    """Whether this machine handed the role over and the successor has now taken it.

    Asked before acting as center, so the outgoing center cannot go on signing lists
    the fleet no longer follows. Costs nothing unless a handover is outstanding.
    """
    from .ops.handover import settle

    return settle(acc)


def _skills_note() -> str:
    """Say so when this machine's agents read an older description of fleet.

    In the human view of `fleet ls` only, the command people actually look at. An update
    refreshes the skills itself now, so this mostly catches a fleet upgraded by hand.
    Never fatal: it is advice, and a read must not fail over it.
    """
    try:
        root = Path.home()
        stale = stale_targets(root, fleet_command()) + stale_mcp_clients(
            root, config_command())
    except Exception:
        return ""
    if not stale:
        return ""
    return (f"{', '.join(stale)} read{'s' if len(stale) == 1 else ''} an older "
            "description of fleet -- [bold]fleet setup --refresh[/bold]")


def _refuse_while_handing_over(acc) -> None:
    """Refuse a change on a center that has handed the role over but not heard back.

    Found on a real fleet: the outgoing center, with no word yet from its successor,
    went on issuing invites. The successor took a copy of the list at handover, so
    anything changed here in between is simply lost when it accepts -- silently.
    """
    if not acl.HANDING_PATH.exists() or _stepped_down(acc):
        return
    try:
        to = (yaml.safe_load(acl.HANDING_PATH.read_text(encoding="utf-8")) or {}).get("to_name", "the successor")
    except (OSError, yaml.YAMLError):
        to = "the successor"
    err.print(f"[red]The role is being handed to {to},[/red] so this machine makes no "
              "changes meanwhile -- they would not carry over.")
    err.print(f"  [dim]on {to}: [bold]fleet center --accept[/bold] (it needs to be "
              f"listening for this machine to notice). To keep the role instead: "
              f"[bold]fleet center --cancel[/bold] here.[/dim]")
    raise typer.Exit(2)


def _not_in_a_fleet(json_out: bool) -> None:
    if _emit({"role": "", "is_center": False, "member": False}, json_out):
        return
    err.print("[red]This machine is not in a fleet.[/red]")
    err.print("  [dim]start one here with [bold]fleet center --init[/bold], or join one "
              "with the code [bold]fleet invite[/bold] prints on its center[/dim]")
    raise typer.Exit(2)


def _center_off_the_center(*, leave: bool, dissolve: bool, accept: bool, export: bool,
                           name: str | None, json_out: bool) -> None:
    """`fleet center` on a machine that holds no access list: a member, or nothing yet.

    Answered from what a member does hold, rather than by refusing: `--json` has to be
    able to say `is_center: false`, since that is how an agent asks, and leaving is a
    member's own right.
    """
    from .ops import member

    if _fleet_membership() != "member":
        _not_in_a_fleet(json_out)
        return
    center = member.center_name()
    if leave:
        fid, removed = member.leave()
        console.print(f"[green]✓[/green] left fleet {fid or '(unknown)'}: removed "
                      f"{removed} key block(s) from this machine's authorized_keys, and "
                      "forgot the center")
        console.print(f"  [dim]{center} will see this machine as unreachable until you "
                      f"remove it there with [bold]fleet rm[/bold][/dim]")
        return
    if accept:
        with _as_exit():
            _accept_handover(None)
        return
    if dissolve or export or name:
        what = ("dissolve the fleet" if dissolve else "export the access list" if export
                else "hand the role over")
        err.print(f"[red]Only the center can {what}.[/red] This machine is a member; "
                  f"run it on [bold]{center}[/bold].")
        raise typer.Exit(2)
    info = member.status()
    if _emit(info, json_out):
        return
    console.print(f"center   [bold]{center}[/bold]")
    unknown = "[dim]unknown until the center next reaches this machine[/dim]"
    console.print(f"fleet    {info['fleet_id'] or unknown}")
    console.print(f"this     a member — changes to who may reach what are made on "
                  f"{center}")
    if info["last_seen_s"] is not None:
        console.print(f"seen     {_ago(info['last_seen_s'])} ago"
                      + (f" via {info['center_url']}" if info["center_url"] else ""))
    if note := staleness_note():
        console.print(f"[yellow]![/yellow] {note}")


def _ago(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@app.command("setup")
def cmd_setup(
    target: str = typer.Option("auto", "--target",
                               help="an agent name, or all | auto (whatever is installed)"),
    project: bool = typer.Option(False, "--project",
                                 help="write into the current directory, not your home"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would change; write nothing"),
    remove: bool = typer.Option(False, "--uninstall", help="remove what setup installed"),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="rewrite only what fleet already installed, so it "
                                      "matches this fleet; adds nothing new"),
):
    """Teach your coding agents to use fleet.

    An agent with a shell gets a skill, which costs nothing until a task actually needs
    a machine. A desktop client has no shell, so it gets `fleet mcp` registered as an
    MCP server instead -- merged into its config beside whatever else is already there.

    [dim]Example:[/dim]  fleet setup --dry-run
    """
    if not (refresh or remove or dry_run):
        _first_run("setup")
    root = Path.cwd() if project else Path.home()
    # MCP clients are a second namespace: a desktop app is registered, not written to.
    # `--project` never touches them -- their config is per-user, not per-repo.

    mcp_names = tuple(c.name for c in MCP_CLIENTS)
    if refresh:
        # Run by every install and update, on the machine being updated. So it may only
        # rewrite what is already there -- never teach an agent fleet that someone chose
        # to leave alone -- and finding nothing to refresh is not a failure.
        targets = installed_targets(root, project=project)
        clients = [] if project else installed_mcp_clients(root)
        if not targets and not clients:
            console.print("[dim]· no agent here has fleet installed; nothing to refresh"
                          "[/dim]")
            return
        changes = (install(root, targets, fleet_command(), dry_run=dry_run, project=project)
                   + install_mcp(root, clients, config_command(), dry_run=dry_run))
        changed = [c for c in changes if c.action != "unchanged"]
        for c in changed:
            console.print(f"  [green]{c.action:<9}[/green] {c.path}")
        if not changed:
            console.print("[dim]· agent skills already match this fleet[/dim]")
        return
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
        from .agents import registry as agent_registry

        # Each agent's own folder: "~/.opencode" was named for one that lives in ~/.config.
        looked = sorted({"~/" + a.marker for a in agent_registry.AGENTS})
        err.print("[yellow]No coding agent found.[/yellow]  Looked for "
                  f"{', '.join(looked)} and the desktop clients.  "
                  "Force one with [bold]--target claude[/bold].")
        raise typer.Exit(1)

    cmd = fleet_command()
    if remove:
        changes = (uninstall(root, targets, dry_run=dry_run, project=project)
                   + uninstall_mcp(root, clients, dry_run=dry_run))
    else:

        changes = (install(root, targets, cmd, dry_run=dry_run, project=project)
                   + install_mcp(root, clients, config_command(), dry_run=dry_run))

    for c in changes:
        colour = {"created": "green", "updated": "green",
                  "removed": "yellow"}.get(c.action, "dim")
        console.print(f"  [{colour}]{c.action:<9}[/{colour}] {c.path}")
    if dry_run:
        console.print("\n[dim]--dry-run: nothing was written.[/dim]")
    elif not remove and cmd != "fleet":
        err.print(f"\n[dim]A new shell here does not find `fleet` by name, so the "
                  f"skill calls it by its full path, {cmd}. To use the short name: "
                  "[bold]uv tool update-shell[/bold], then [bold]fleet setup[/bold] in a "
                  "new terminal.[/dim]")


@app.command("service")
def cmd_service(action: str = typer.Argument("status",
                                             help="status | install | remove | start | stop")):
    """The background service that keeps the center listening on port 7373.

    Starting a fleet installs it and updates restart it, so you rarely need this: it is
    for looking when something is wrong, putting it back after a handover (the new
    center's `fleet service install`), or removing it.

    [dim]Example:[/dim]  fleet service status
    """

    if action == "status":
        console.print(service.status(DEFAULT_PORT))
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

    from .ops import firstrun

    # A desktop client launching this is someone using fleet here. The line goes to
    # stderr, which clients log, because stdout is the protocol.
    firstrun.maybe("mcp", lambda line: print(f"fleet: {Text.from_markup(line).plain}",
                                             file=sys.stderr))
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

    # Read through the modules that own these, not through this module's import-time
    # copies. `from .config import INVENTORY_PATH` binds the value once, so a test that
    # redirects state -- and `fleet paths` is the one command whose whole job is to say
    # where state lives -- was printing the real machine's paths from inside the sandbox.
    console.print(f"inventory  {inv.INVENTORY_PATH}")
    console.print(f"fleet key  {_cfg.FLEET_KEY}   [dim](never regenerate: it is this "
                  "machine's identity)[/dim]")
    console.print(f"access     {acl.ACCESS_PATH}   [dim](center only — the authority)[/dim]")
    console.print(f"ledger     {acl.LEDGER_PATH}   [dim](center only — what has landed)[/dim]")
    console.print(f"seen       {acl.CACHE_PATH}   [dim](the center's key, and when it "
                  "last swept)[/dim]")
    from .state import invites as _invites
    console.print(f"invites    {_invites.INVITES_PATH}   [dim](center only — open "
                  "invites, as hashes)[/dim]")
    console.print(f"cache      {store.DB_PATH}   [dim](disposable — delete and re-probe)[/dim]")
    if _cfg.CONFIG_DIR == _cfg.STATE_DIR:
        # Worth saying out loud: it is why every filename above is distinct, and why
        # nothing here may ever be cleaned up by globbing a directory.
        console.print(f"\n[dim]config and state are the same directory here "
                      f"({_cfg.CONFIG_DIR}).[/dim]")


def main() -> None:
    """Run the CLI. Failures a person can act on end in one line, not a traceback.

    A malformed inventory or access list, a write that could not get its turn, or a
    machine that never answered used to surface as a Python traceback -- and under
    `--json`, an agent got an empty stdout and no reason. Those now print what went
    wrong (as JSON too, with `--json`) and exit 2. Anything else is a bug, and keeps its
    traceback, which is what a bug report needs.
    """
    import subprocess

    from .state.writes import QueueTimeout

    expected = (inv.InventoryError, acl.AccessError, QueueTimeout,
                subprocess.TimeoutExpired)
    try:
        app()
    except expected as exc:
        message = str(exc) or type(exc).__name__
        if isinstance(exc, subprocess.TimeoutExpired):
            message = f"a machine did not answer within {exc.timeout:.0f}s"
        if "--json" in sys.argv:
            sys.stdout.write(jsonlib.dumps({"ok": False, "error": message}) + "\n")
        err.print(f"[red]fleet:[/red] {message}")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
