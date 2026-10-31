"""fleet CLI. Every read command supports --json, because the CLI -- not MCP -- is the
universal interface: cron jobs, Makefiles, and non-MCP agents can all use it."""

from __future__ import annotations

import contextlib
import dataclasses
import copy
import json as jsonlib
import math
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
from . import notes as notes_mod
from .agents import (MCP_CLIENTS, TARGETS, config_command, detect_mcp_clients,
                     detect_targets, fleet_command, fleet_executable, install,
                     install_mcp, installed_mcp_clients, installed_targets, package_version,
                     plan, stale_mcp_clients, stale_targets, uninstall, uninstall_mcp)
from .config import INVENTORY_PATH, load_config
from .edit import apply_edits, check_name, is_full_path
from .install import (NOTHING_TO_UPDATE, build_install_argv, install_script, needs_agent,
                      install_source,
                      local_install_argv, payload_for)
from .mcpserver import McpUnavailable, serve as serve_mcp
from .models import Device, Kind, Status
from .onboard import derive_id, onboard, onboard_self
from .ops import FleetError, identity
from .ops.names import canonical as _canonical
from .ops.remote import run_script as _run_script
from .ops import rows as rows_mod
from .ops.rows import snapshot as _rows, tick as _live_tick
from .probe.runner import PAYLOAD, probe_env, probe_many, run_probe, run_probe_local
from .render import view as view_mod
from .render.top import (Schedule, device_lines, disk_cell, gpu_cells_compact, render_models,
                         render_ps, runtime as runtime_text, size as size_text, reading_age,
                         name_cell, render_device, render_fleet, render_ls, _gb as gb_text)
from .render.view import Detail, auth_of, device_view, fleet_view, matches_tag
from .ssh.cmd import (WINDOWS, build_argv, jump_args, local_platform, local_shell_argv,
                      remote_command, run as sshrun,
                      parse_ssh_command, remote_platform, resolve_command)
from .state import inventory as inv
from .state import store
from .ui import (DOT as _DOT, chatter_to_stderr as _chatter_to_stderr, console,
                 emit as _emit, err)

app = typer.Typer(
    add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
    help="Every machine you have, for you and your agents: GPUs and what is running on "
         "them, models served, software, and where models and data live.",)
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
    """Once per machine: record it, and teach the agents here. See ops.firstrun."""
    from .ops import firstrun

    firstrun.maybe(command, lambda line, level="ok": err.print(f"{_MARK[level]} {line}"))


# How a line reports its outcome: done, done but worth a look, or not done.
_MARK = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}


@app.command("ls")
def cmd_ls(names: list[str] = typer.Argument(None, help="only these devices"),
           json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
           refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe"),
           rescan: bool = typer.Option(False, "--rescan", help="also re-read software versions and folder sizes, which machines otherwise cache for 6 hours"),
           wait: int = typer.Option(0, "--wait", metavar="SECONDS",
                                    help="with --rescan: wait up to this long for the folder sizes it started measuring"),
           online: bool = typer.Option(False, "--online", help="only reachable devices"),
           discover: bool = typer.Option(False, "--discover",
                                         help="list machines in your ~/.ssh/config and on "
                                              "your tailnet that are not added yet"),
           hide: list[str] = typer.Option(None, "--hide", metavar="NAME",
                                          help="with --discover: never offer this one again; "
                                               "repeatable"),
           unhide: list[str] = typer.Option(None, "--unhide", metavar="NAME",
                                            help="with --discover: offer it again"),
           show_all: bool = typer.Option(False, "--all",
                                         help="with --discover: include hidden ones"),
           fields: str = typer.Option(None, "--fields", metavar="A,B,...",
                                      help="with --json: only these keys of each machine, "
                                           "e.g. free_vram_mib,gpus,usd_per_hour"),
           tag: list[str] = typer.Option(None, "--tag", metavar="NAME",
                                         help="only machines carrying this tag or fact; "
                                              "repeatable, and all must match")):
    """List every device with live resource availability.

    [dim]Example:[/dim]  fleet ls --json
    """
    if tag and not view_mod.split_tags(tag):
        # `--tag ""` listed every machine, as though each had matched.
        err.print(f"[red]--tag {escape(repr(tag[0]))}[/red] is not a tag  [dim]e.g. "
                  "--tag cuda, or --tag cuda,vram-24g for both[/dim]")
        raise typer.Exit(2)
    if fields is not None and not any(f.strip() for f in fields.split(",")):
        # `--fields ""` printed every field, as though none had been asked for.
        err.print("[red]--fields needs the keys to keep[/red]  [dim]e.g. --fields "
                  "name,status,free_vram_mib[/dim]")
        raise typer.Exit(2)
    _first_run("ls")
    if discover or hide or unhide:
        _print_discovered(json_out, hide=hide or [], unhide=unhide or [], show_all=show_all)
        return
    if show_all:
        # Taken in silence, it read as "these are all of them" -- hidden ones included.
        err.print("[dim]· --all applies to --discover[/dim]")
    # A name that matches nothing is said, and fails: `fleet ls TYPO --json` answered an
    # empty list and exit 0, which an agent reads as "nothing wrong, nothing there".
    _refuse_unknown(names, json_out)
    rows = _rescanned(list(names) if names else None, refresh=refresh, rescan=rescan,
                      wait=wait, detail=Detail.COMPACT)
    known_rows = bool(rows)
    if online:
        rows = [r for r in rows if r["status"] == "ok"]
    tag = view_mod.split_tags(tag)
    if tag:
        # Say what could not be judged rather than dropping it silently. A machine with no
        # telemetry has no facts, so it fails every filter -- and the fleet is factless
        # right after a cache wipe or a schema bump. Shared hosts are worse: `_rows` only
        # probes `probeable`, and SHARED is forced to on_demand at onboarding because one
        # cluster hangs ~75s, so a bare `fleet ls --tag gpu` judges it on nothing while
        # `fleet ls koa04 --tag gpu` probes and answers differently.
        # A machine with tags of its own but no reading is just as blind to `--tag cuda`:
        # counting only the untagged ones left it out of the count as well as the list.
        # So is one whose only fact is its kind -- `rental`, `shared`, `appliance` come
        # from the list, not a reading: a fleet of rentals never reached went uncounted.
        # Those facts, asked for, are judged all the same: `--tag rental` needs no reading,
        # and a machine that is not a rental fails `--tag rental --tag cuda` on that alone.
        listed = [t for t in tag if view_mod.normalise_tag(t) in view_mod.LISTED_FACTS]
        blind = [r["name"] for r in rows
                 if not (r.get("observed_at") or {}).get("probe")
                 and all(matches_tag(r, t) for t in listed)
                 and not all(matches_tag(r, t) for t in tag)]
        rows = [r for r in rows if all(matches_tag(r, t) for t in tag)]
        if blind:
            # "1 ... were", and a seventh one cut from the list without a word.
            more = f" and {len(blind) - 6} more" if len(blind) > 6 else ""
            err.print(f"[dim]! {len(blind)} with no telemetry "
                      f"{'was' if len(blind) == 1 else 'were'} not considered: "
                      f"{', '.join(sorted(blind)[:6])}{more}[/dim]")
    view = fleet_view(rows)
    if fields and json_out:
        wanted = [f.strip() for f in fields.split(",") if f.strip()]
        view = {**view, "devices": [view_mod.select_fields(r, wanted) for r in rows]}
        # A typo used to come back as rows holding only `name`, which reads as "no
        # machine has this" rather than "no such field".
        known = {k for r in rows for k in r}
        if rows and (unknown := [f for f in wanted if f not in known]):
            err.print(f"[yellow]--fields: no such field: {', '.join(unknown)}[/yellow]  "
                      f"[dim]fields: {', '.join(sorted(known))}[/dim]")
    elif fields:
        err.print("[dim]· --fields applies to --json[/dim]")
    if _emit(view, json_out):
        return
    if not rows and known_rows:
        # "No devices yet" with machines in the list sent people to add one they had.
        asked = [f"--tag {t}" for t in tag] + (["--online"] if online else [])
        err.print(f"[yellow]No machine matches {escape(' '.join(asked))}.[/yellow]  "
                  "[dim]`fleet ls` lists them all[/dim]")
        raise typer.Exit(0)
    if not rows:
        err.print("[yellow]No devices yet.[/yellow]  Add one:  "
                  "[bold]fleet add \"ssh user@host\"[/bold]")
        raise typer.Exit(0)

    t = render_ls(rows, width=console.width)
    console.print(t)
    if asleep := [r["name"] for r in rows if r.get("wakeable")]:
        console.print(f"[dim]· asleep? {escape(', '.join(asleep[:3]))}"
                      f"{f' and {len(asleep) - 3} more' if len(asleep) > 3 else ''} can be "
                      f"woken: fleet wake {escape(asleep[0])}[/dim]")
    if note := _skills_note():
        console.print(f"[yellow]![/yellow] [dim]{note}[/dim]")
    s = view["summary"]
    console.print(f"\n[dim]{s['online']}/{s['total']} online · {s['gpus_free']} free GPU(s)"
                  + (f" · {view_mod.usd(s['hourly_burn'])}/hr burning" if s["hourly_burn"] else "") + "[/dim]")


def _rescanned(names: list[str] | None, *, refresh: bool, rescan: bool, wait: int,
               detail: Detail) -> list[dict]:
    """Rows, and with `--rescan --wait N`, rows once the folder sizes are new.

    The sizes come from a `du` the rescan starts in the background on each machine -- a
    probe never waits on it, because on a disk of model weights it can take minutes. So
    this waits here instead, re-reading every two seconds until every machine's sizes are
    newer than they were before (compared with themselves, not with this machine's clock,
    which the other one's need not agree with), or until the time is up.
    """
    if wait < 0:
        # `--wait -5` was taken as no wait at all, without a word.
        err.print(f"[red]--wait {wait}[/red] is not a wait  [dim]seconds, 0 or more[/dim]")
        raise typer.Exit(2)
    if wait > 0 and not rescan:
        # Nothing was measured to wait for, and it returned at once without a word.
        err.print("[dim]· --wait applies to --rescan[/dim]")
    if not (rescan and wait > 0):
        return _rows(names, refresh=refresh, rescan=rescan, detail=detail)
    devices = inv.live(inv.load())
    if names:
        devices = [d for d in (inv.find(devices, n) for n in names) if d]

    def stamps() -> dict[str, set]:
        """Each answering machine's folders and when each was sized. One that is not
        answering is left out: waiting for it would only use up the time."""
        conn = store.connect()
        try:
            out = {}
            for d in devices:
                state, snap = store.latest(conn, d.id)
                if (state or {}).get("status") != Status.OK.value:
                    continue
                out[d.id] = {(st.get("path"), st.get("sized_at"))
                             for st in (snap or {}).get("storage", []) if st.get("exists")}
            return out
        finally:
            conn.close()

    before = stamps()
    rows = _rows(names, refresh=True, rescan=True, detail=detail)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        now = stamps()
        pending = [d for d in devices if now.get(d.id) and any(
            at is None or (path, at) in before.get(d.id, set()) for path, at in now[d.id])]
        if not pending:
            break
        time.sleep(2)
        rows = _rows(names, refresh=True, detail=detail)
    else:
        err.print(f"[dim]! folder sizes still being measured after {wait}s; they show on a "
                  "later reading[/dim]")
    return rows


def _print_discovered(json_out: bool, *, hide: list[str] = (), unhide: list[str] = (),
                      show_all: bool = False) -> None:
    """`fleet ls --discover`: what your ssh config and tailnet know that fleet does not."""
    from .ops import discover

    for n in [*hide, *unhide]:
        # The list is one name a line, and a line starting with `#` is a comment: a
        # name with a newline in it hid two, and one starting with `#` hid nothing,
        # both under a ✓-less silence that read as done.
        if not n.strip() or any(c.isspace() for c in n.strip()) or n.strip().startswith("#"):
            err.print(f"[red]{escape(repr(n))} is not a name --discover offers[/red]")
            raise typer.Exit(2)
    hide = [n.strip() for n in hide]
    unhide = [n.strip() for n in unhide]
    if unhide and (unknown := [n for n in unhide if n.lower() not in discover.hidden()]):
        err.print(f"[yellow]not hidden: {escape(', '.join(unknown))}[/yellow]  "
                  "[dim](fleet ls --discover --all lists what is)[/dim]")
    if hide:
        discover.set_hidden(list(hide), hide=True)
    if unhide:
        discover.set_hidden(list(unhide), hide=False)
    found = discover.candidates(include_hidden=True)
    gone = discover.hidden()

    def is_hidden(c) -> bool:
        # Under --all a hidden one was listed like any other, with nothing to say which.
        return c.name.lower() in gone or c.target.lower() in gone

    offered = {n for c in found for n in (c.name.lower(), c.target.lower())}
    if hide and (unseen := [n for n in hide if n.lower() not in offered]):
        # `--hide old-bx` hid nothing anyone sees, old-box stayed on the list, and the
        # answer was the list itself: a typo read as a hide that had not taken.
        err.print(f"[yellow]not offered now: {escape(', '.join(unseen))}[/yellow]  "
                  "[dim](hidden all the same, should it turn up; --unhide undoes it)[/dim]")
    if not show_all:
        found = [c for c in found if not is_hidden(c)]

    if _emit({"candidates": [{**c.to_dict(), "hidden": is_hidden(c)} for c in found],
              "hidden": sorted(gone)}, json_out):
        return
    new = [c for c in found if not c.added]
    if not found:
        console.print("[dim]Nothing found: no Host entries in ~/.ssh/config, and no "
                      "tailnet (tailscale status) on this machine.[/dim]")
        return
    t = Table(box=None, pad_edge=False, header_style="bold")
    for col in ("NAME", "FROM", "OS", "ONLINE", "ADD WITH"):
        t.add_column(col, no_wrap=True)
    for c in new:
        t.add_row(c.name + (" [dim](hidden)[/dim]" if is_hidden(c) else ""), c.source,
                  c.os or "-",
                  "-" if c.online is None else ("yes" if c.online else "[dim]no[/dim]"),
                  f"fleet add {c.name if c.source == 'ssh-config' else c.target}")
    if new:
        console.print(t)
    added: dict[str, list[str]] = {}
    for c in found:
        if c.added:
            added.setdefault(c.added, []).append(c.name + (" (hidden)" if is_hidden(c) else ""))
    if added:
        # Six Host entries for one machine read "lab, lab, lab, lab, lab, lab".
        console.print("\n[dim]already added: " + ", ".join(
            name + (f" (as {', '.join(n for n in via if n != name)})"
                    if any(n != name for n in via) else "")
            for name, via in added.items()) + "[/dim]")
    if gone and not show_all:
        console.print(f"[dim]hidden ({len(gone)}): --all lists them, --unhide NAME offers "
                      "one again[/dim]")
    elif show_all and (unseen := sorted(gone - {c.name.lower() for c in found}
                                        - {c.target.lower() for c in found})):
        # --all was said to list them, and a hidden name no longer found listed nothing.
        console.print(f"[dim]hidden, and not found now: {escape(', '.join(unseen))}[/dim]")


@app.command("show")
def cmd_show(name: str = typer.Argument(None, help="defaults to this machine"),
             json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
             refresh: bool = typer.Option(True, "--refresh/--no-refresh", "-r", help="probe it now; --no-refresh shows the last reading"),
             rescan: bool = typer.Option(False, "--rescan", help="also re-read software versions and folder sizes, which machines otherwise cache for 6 hours"),
             wait: int = typer.Option(0, "--wait", metavar="SECONDS",
                                      help="with --rescan: wait up to this long for the folder sizes it started measuring")):
    """Full detail for one device.

    [dim]Example:[/dim]  fleet show machine_A
    """
    _first_run("show")
    unrecorded = False
    if name is None:
        me = identity.local_device_id()
        if not me or inv.find(inv.load(), me) is None:
            # Nothing set up yet. The first command anyone runs -- often through
            # `uvx`, before installing anything -- used to answer "not in the
            # inventory, so there is nothing to show". Show the machine anyway: one
            # local probe, kept in the disposable cache, the inventory left alone.
            r, unrecorded = _show_unrecorded_self(), True
        else:
            name = _this_machine(inv.load(), "show").name
    if not unrecorded:
        rows = _rescanned([name], refresh=refresh, rescan=rescan, wait=wait,
                          detail=Detail.FULL)
        if not rows:
            _refuse_unknown([name], json_out)
            err.print(f"[red]No device named {name!r}.[/red]  Try [bold]fleet ls[/bold]")
            raise typer.Exit(1)
        r = rows[0]
    if _emit(r, json_out):
        return
    _print_show(r)
    if unrecorded:
        console.print("\n  [dim]this machine is not recorded: [bold]fleet add --self"
                      "[/bold] records it[/dim]")


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
    # A machine never reached has no OS or arch yet: "timeout · ? · " ended on nothing.
    head = [r["kind"], r["status"], r.get("os") or "", r.get("arch") or ""]
    console.print(f"\n{_DOT.get(r['status'],'?')} [bold]{escape(r['name'])}[/bold]  "
                  f"[dim]{escape(' · '.join(h for h in head if h))}[/dim]")
    if r.get("error"):
        console.print(f"  [red]{r['error']['class']}[/red]: {escape(r['error']['detail'] or '')}")
    # A machine fleet does not probe now -- mobile, or one that backs off -- showed a
    # reading hours old as though it had just been taken.
    age = reading_age(r)
    if age is not None and age > int(load_config().get("telemetry_ttl_s") or 0):
        why = ("; fleet never probes a mobile device" if r.get("probe_policy") == "never"
               else "")
        console.print(f"  [dim]as read {_ago(int(age))} ago{why}[/dim]")
    if r.get("cpu_model"):
        console.print(f"  cpu   {r['cpu_cores']}x {escape(r['cpu_model'])}   load {r.get('load')}")
    if r.get("ram_total_gb"):
        console.print(f"  ram   {r['ram_free_gb']:.1f} / {r['ram_total_gb']:.1f} GB free")
    for g in r["gpus"]:
        bar = "█" * (g["util_pct"] // 10) + "░" * (10 - g["util_pct"] // 10)
        console.print(f"  gpu{g['idx']}  {escape(g['name'])}  {bar} {g['util_pct']}%   "
                      + ("memory shared with the cpu: ram above" if g.get("shared_memory") else
                         f"{g['vram_free_mib']/1024:.1f}/{g['vram_total_mib']/1024:.1f} GB free"))
        if g["display_used_mib"]:
            console.print(f"        [dim]{g['display_used_mib']} MiB desktop overhead (not a job)[/dim]")
        if g["unattributed_mib"]:
            console.print(f"        [yellow]{g['unattributed_mib']} MiB unattributed[/yellow]")
    for d in r.get("disks", []):
        # "1048576 GB free" for a 1 PiB volume: the same units as `fleet ls`. And a
        # read-only mount says so, or its "0 GB free" reads as a full disk.
        console.print(f"  disk  {escape(str(d['mount'])):<12} {gb_text(d['free_gb'])} free"
                      + (f"  [dim]{d['use_pct']}% used[/dim]" if d.get("writable", True)
                         else "  [dim]read-only[/dim]"))
    if r.get("processes"):
        console.print("  [bold]gpu compute[/bold]")
        # As wide as the longest name: `_windowserver`, or a login past ten letters now
        # that the probe keeps whole names, pushed its row out of line with the rest.
        uw = max(10, *(len(p["user"] or "?") for p in r["processes"]))
        # The card each is on, in the column the `gpuN` lines above it are named in.
        card = lambda p: f"{'gpu' + str(p['gpu']) if p.get('gpu') is not None else '':<6}"
        for p in r["processes"]:
            # As in `fleet ps`: a job in a container shows no user, age or command from
            # here, and "0m" with a blank said it had just started and was nothing.
            if not p["user"] and not (p.get("args") or p.get("comm")):
                console.print(f"  {card(p)}{p['pid']:>8} {'?':<{uw}} {p['vram_mib']:>6} MiB  {'-':>6}  "
                              "[dim]not visible from this login (a container?)[/dim]")
                continue
            console.print(f"  {card(p)}{p['pid']:>8} {escape(p['user'] or '?'):<{uw}} {p['vram_mib']:>6} MiB  "
                          f"{runtime_text(p.get('etimes', 0)):>6}  {escape((p.get('args') or p['comm'])[:90])}")
        if r.get("processes_more"):
            console.print(f"        [dim]and {r['processes_more']} more: "
                          f"fleet ps {escape(r['name'])} lists them all[/dim]")
    elif r["gpus"] and r.get("kind") == "shared":
        # Its processes are left unread on purpose: "none -- no job is using this GPU"
        # under a login node's card at 97% holding 20G.
        console.print("  [dim]gpu compute   not listed: fleet does not read a shared host's processes[/dim]")
    elif r["gpus"]:
        console.print("  [dim]gpu compute   none — no job is using this GPU[/dim]")
    if r.get("top_cpu"):
        console.print("  [bold]top cpu[/bold]")
        uw = max(10, *(len(p["user"] or "?") for p in r["top_cpu"]))
        for p in r["top_cpu"]:
            console.print(f"        {p['pid']:>8} {escape(p['user'] or '?'):<{uw}} {p['cpu_pct']:>5}%  {escape(p['comm'] or '')}")
    if sl := r.get("slurm"):
        jobs = (f"{sl['my_jobs']} job(s) of yours, {sl['my_running']} running"
                if sl.get("my_jobs") is not None else "your jobs unknown")
        console.print(f"  [bold]slurm[/bold]  {jobs}")
        for pt in sl.get("partitions") or []:
            console.print(f"        {escape(pt['name']):<12} {escape(pt['avail']):<5} "
                          f"{pt['nodes'] if pt['nodes'] is not None else '?':>4} node(s) "
                          f"{escape(pt['state'])}" + ("  [dim]default[/dim]" if pt["default"] else ""))
    if r.get("services_detail"):
        console.print("  [bold]services[/bold]")
        for s in r["services_detail"]:
            console.print(f"        :{s['port']:<6} {escape(s.get('kind') or '?'):<10} {escape(s.get('comm') or '')}")
    def _more(m, shown):
        # Six names here, twenty kept: either way, say that there were more.
        more = max(m.get("models_total") or 0, len(m["models"])) - min(shown, len(m["models"]))
        return f" [dim]and {more} more[/dim]" if more > 0 else ""

    for m in r.get("models", []):
        down = m.get("reach") == "down"
        where = "nothing answers there" if down else m["url"] or "local only"
        console.print(f"  [bold]models[/bold] :{m['port']} {escape(m['framework'] or '')} [dim]{escape(where)}[/dim]  "
                      + (escape(", ".join(m["models"][:6])) + _more(m, 6) if m["models"]
                         else "[yellow]wants an API key[/yellow]" if m["status"] == "auth"
                         else "[yellow]not running[/yellow]" if down
                         else "[yellow]no model list there[/yellow]" if m["status"] == "none"
                         else "[dim]none listed[/dim]"))
    sw = r.get("software") or {}
    if sw.get("system"):
        console.print("  [bold]software[/bold] " + "  ".join(
            f"{k} [dim]{escape(v)}[/dim]" for k, v in sw["system"].items()))
    for e in sw.get("envs", []):
        console.print(f"        [dim]{escape(e['env'])}[/dim]"
                      + (f" py{e['python']}" if e.get("python") else "") + "  "
                      + "  ".join(f"{k} {escape(v)}" for k, v in e["packages"].items()))
    for c in sw.get("containers") or []:
        console.print(f"        [dim]container[/dim] {escape(c['name'])} "
                      f"[dim]{escape(c['image'])} · {escape(c['status'])}[/dim]")
    if (more := (sw.get("containers_total") or 0) - len(sw.get("containers") or [])) > 0:
        console.print(f"        [dim]and {more} more containers running[/dim]")
    for kind, items in (r.get("storage") or {}).items():
        for st in items:
            if not st["exists"]:
                console.print(f"  {escape(kind):<9} {escape(st['path'])} [yellow]missing[/yellow]")
                continue
            size = f"{size_text(st['size_gb'])} used" if st["size_gb"] is not None else "size pending"
            free = f", {st['free_gb']:.0f} GB free" if st["free_gb"] is not None else ""
            console.print(f"  {escape(kind):<9} {escape(st['path'])} [dim]{size}{free}[/dim]")
    # Both lists, labelled and apart, because this is the one surface with room for them
    # and the one a human reads when deciding. `tags` is what you said; `facts` is what
    # the last probe measured, and the two must stay distinguishable.
    if r.get("tags"):
        console.print(f"  tags  {escape(' '.join(map(str, r['tags'])))}")
    if r.get("facts"):
        console.print(f"  facts [dim]{escape(' '.join(map(str, r['facts'])))}[/dim]")
    for a in r["alerts"]:
        console.print(f"  [yellow]![/yellow] {escape(a)}")
    c = r["connect"]
    console.print(f"\n  [bold]connect[/bold]  {escape(c.get('ssh_command') or c.get('hint') or '')}")
    if r.get("notes"):
        console.print(f"  [dim]{escape(r['notes'].strip())}[/dim]")


def _edit_machine_file(dev, json_out: bool, *, paths: list[str], endpoints: list[str],
                       note: str | None, tags_add: list[str], tags_remove: list[str],
                       cost: float | None, emit: bool = True) -> dict | None:
    """Change the machine's own machine.yaml: folders, declared servers, tags, cost, note.

    Never falls back to this machine's list when the machine cannot be reached: a change
    that silently landed somewhere else would read back as done, and be invisible to
    everyone else who looks at that machine. `--local` is how to ask for that.

    With `emit=False` and --json, the document is returned instead of printed, for the
    caller to fold into its own: `--json` prints one document, whatever else changed.
    """
    from .ops import machinefile

    wanted: dict[str, list[str] | None] = {}
    for spec in paths:
        kind, sep, value = spec.partition("=")
        kind = kind.strip().lower()
        if not sep or not notes_mod.valid_kind(kind):
            err.print(f"[red]--path {escape(repr(spec))}[/red] is not KIND=PATH  "
                      "[dim]e.g. --path models=/data/models (kinds: "
                      f"{', '.join(notes_mod.KINDS)}, or your own)[/dim]")
            raise typer.Exit(2)
        if value.strip() and not is_full_path(value):
            # `models=data/models` was kept as written: relative to wherever the probe's
            # shell started, shown as is beside `~/m` shown in full, and no use to an
            # agent working anywhere else.
            err.print(f"[red]--path {escape(repr(spec))}[/red]: {escape(value.strip())} is "
                      "not a full path  [dim]e.g. /data/models, ~/models or D:\\models[/dim]")
            raise typer.Exit(2)
        if value.strip():
            # The same folder twice was recorded twice, and counted twice in its size.
            items = wanted.setdefault(kind, []) or []
            if value.strip() not in items:
                items.append(value.strip())
            wanted[kind] = items
        else:
            wanted[kind] = None
    servers: dict[str, str | None] = {}
    for spec in endpoints:
        name, sep, value = spec.partition("=")
        name = name.strip().lower()
        if sep and notes_mod.valid_kind(name) and (port := re.match(r"\s*(\d+)", value)) \
                and not 1 <= int(port.group(1)) <= 65535:
            # Port 0 or 70000 said the whole thing was not NAME=PORT, which it was.
            err.print(f"[red]--endpoint {escape(repr(spec))}[/red]: port "
                      f"{int(port.group(1))} is not a port  [dim]ports run from 1 to "
                      "65535[/dim]")
            raise typer.Exit(2)
        if not sep or not notes_mod.valid_kind(name) or (
                value.strip() and notes_mod.endpoint_spec(value) is None):
            err.print(f"[red]--endpoint {escape(repr(spec))}[/red] is not "
                      f"{escape('NAME=PORT[/PATH]')}  "
                      "[dim]e.g. --endpoint myllm=9000 or myllm=9000/v1; myllm= removes "
                      "it[/dim]")
            raise typer.Exit(2)
        servers[name] = value.strip() or None
    here = bool(identity.local_device_id()) and dev.id == identity.local_device_id()
    if not here:
        # `--path models=~/models` reaches us with `~` already turned into *this* machine's
        # home by the shell. On the machine the path is for, it meant that one's.
        home = str(Path.home()).rstrip("/\\")
        for kind, items in wanted.items():
            for i, item in enumerate(items or []):
                if item == home or item.startswith((home + "/", home + "\\")):
                    items[i] = "~" + item[len(home):].replace("\\", "/")
                    err.print(f"[dim]· {item} is this machine's home folder; kept as "
                              f"{items[i]}, {dev.name}'s own[/dim]")
    conn = store.connect()
    try:
        state, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    eps = inv.by_use(dev, state)
    if not here and not eps:
        err.print(f"[red]{dev.name} has no address to reach it at[/red]")
        raise typer.Exit(1)
    try:
        before, after = machinefile.change(
            None if here else eps[0], machinefile.platform_of(snap, here=here),
            paths=wanted or None, endpoints=servers or None, note=note,
            tags_add=tags_add, tags_remove=tags_remove,
            cost=cost if cost else None, clear_cost=cost == 0)
    except (machinefile.MachineFileError, notes_mod.NotesError) as exc:
        hint = ({} if isinstance(exc, notes_mod.TooLong) else
                {"hint": "--local keeps tags and cost in this machine's own list"})
        if not _emit({"ok": False, "name": dev.name, "error": str(exc), **hint}, json_out):
            err.print(f"[red]Could not change {dev.name}'s machine.yaml:[/red] {escape(str(exc))}")
            if (tags_add or tags_remove or cost is not None) and not isinstance(
                    exc, notes_mod.TooLong):
                err.print("  [dim]to keep tags or cost here instead: add --local[/dim]")
        raise typer.Exit(1)
    result = notes_mod.parse(after)
    if before != after:
        conn = store.connect()
        try:
            store.expire(conn, dev.id)
        finally:
            conn.close()
    had = notes_mod.parse(before).get("endpoints", {})
    doc = {"ok": True, "name": dev.name, "changed": before != after, **result}
    if missing := [n for n, v in servers.items() if v is None and n not in had]:
        doc["not_declared"] = missing         # to remove, and never there: a typo, likely
    if json_out:
        if emit:
            _emit(doc, True)
        return doc
    if before == after:
        # With more to do here (a tag kept with --local, being removed), the answer is
        # what that changed: "machine.yaml already says that" read as nothing done.
        if not emit:
            return doc
        if tags_remove and not (paths or endpoints or tags_add or note is not None
                                or cost is not None):
            # One set by the machine's admin in /etc/fleet: "has no tag" while `ls`
            # showed it, and fleet does not write that file.
            mine = notes_mod.parse(before).get("tags") or []
            admins = [t for t in tags_remove if t not in mine
                      and t in ((snap or {}).get("notes") or {}).get("tags", [])]
            if admins:
                console.print(f"[yellow]· {escape(', '.join(admins))} is set machine-wide "
                              f"on {dev.name}[/yellow]  [dim]in the machine.yaml its admin "
                              "keeps (/etc/fleet); fleet edit writes only yours[/dim]")
            else:
                console.print(f"[dim]· {dev.name} has no tag "
                              f"{escape(', '.join(tags_remove))}.[/dim]")
        elif (servers and all(v is None for v in servers.values())
              and not (paths or tags_add or tags_remove or note is not None
                       or cost is not None)):
            # A misspelt name to remove said "already says that", and the server stayed.
            console.print(f"[dim]· {dev.name} declares no server "
                          f"{escape(', '.join(servers))}.[/dim]")
        else:
            console.print(f"[dim]· {dev.name}'s machine.yaml already says that.[/dim]")
        return doc
    for kind in wanted:
        items = result.get("paths", {}).get(kind)
        console.print(f"[green]✓[/green] {dev.name} {kind}: "
                      + (escape(", ".join(items)) if items else "[dim]cleared[/dim]"))
    for name in servers:
        ep = result.get("endpoints", {}).get(name)
        if not ep and name not in had:
            console.print(f"[dim]· {dev.name} declares no server {escape(name)}.[/dim]")
            continue
        console.print(f"[green]✓[/green] {dev.name} server {escape(name)}: "
                      + (escape(f":{ep['port']}{ep['path']}") if ep else "[dim]removed[/dim]"))
    if tags_add or tags_remove:
        console.print(f"[green]✓[/green] {dev.name} tags: "
                      f"{escape(' '.join(map(str, result.get('tags', [])))) or '-'}")
    if cost is not None:
        c = (result.get("cost") or {}).get("usd_per_hour")
        console.print(f"[green]✓[/green] {dev.name} cost: " + (f"{view_mod.usd(c)}/hr" if c else "cleared"))
    if note is not None:
        console.print(f"[green]✓[/green] {dev.name} note: " + escape(result.get("notes") or "cleared"))
    console.print("  [dim]kept in machine.yaml on the machine itself, so everyone who looks "
                  "at it reads the same[/dim]")
    return doc


def _no_such(devices, name: str):
    """Not found -- and, where a prefix fits several machines, which ones it could be."""
    err.print(f"[red]No device named {name!r}[/red]")
    if near := inv.near_matches(devices, name):
        err.print(f"  [dim]did you mean: {', '.join(near)}[/dim]")
    raise typer.Exit(1)


def _tags(values, *, untag: bool = False) -> list[str] | None:
    """Normalise a repeatable --tag/--untag into a clean list, or None if unmentioned.

    Lowercased because the request that prompted tags said "GPU", "NAS" and "IP" while
    every derived fact is lowercase -- `--tag GPU` matching nothing would be the first
    thing anyone hit.
    """
    if not values:
        return None
    tags = view_mod.split_tags(values)
    if untag:
        # A tag stored whole before tags were split -- "training,big" -- can still go.
        tags += [w for w in map(view_mod.normalise_tag, values) if w and w not in tags]
    if not tags:
        # `--tag ""` said "machine.yaml already says that", as though it had been kept.
        flag = "--untag" if untag else "--tag"
        err.print(f"[red]{flag} {escape(repr(values[0]))}[/red] is not a tag  "
                  f"[dim]e.g. {flag} training, or {flag} a,b for two[/dim]")
        raise typer.Exit(2)
    return tags




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
    """Add a machine: how you reach it over ssh, or a name from `fleet ls --discover`.

    fleet connects exactly as you do -- your ssh config, your keys and agent, Tailscale
    SSH, the key on your rental provider's account -- and installs nothing there.

    [dim]Example:[/dim]  fleet add "ssh -p 58418 root@1.2.3.4"
    """
    if this_machine == bool(ssh_command):
        err.print("[red]Give an ssh command, or --self -- not both, not neither.[/red]")
        raise typer.Exit(2)
    if name is not None and not name.strip():
        # `--name ""` was taken as no --name at all, and the host's name used instead.
        err.print("[red]--name needs a name[/red]  [dim]or leave it out, and fleet names "
                  "it after the host[/dim]")
        raise typer.Exit(2)
    _usable_names(name=name, alias=alias)
    if kind is not None:
        kind = kind.strip().lower()
        if kind not in {k.value for k in Kind}:
            # It was refused only after the machine had been reached, as "'weird' is
            # not a valid Kind" -- the name of a class, and no word of what would do.
            err.print(f"[red]--kind {escape(repr(kind))}[/red] is not a kind  [dim]one "
                      f"of {', '.join(k.value for k in Kind)}[/dim]")
            raise typer.Exit(2)
    # Once what was asked is known to make sense: `fleet add ... --kind weird` recorded
    # this machine and said "✓ fleet is ready here" before refusing.
    _first_run("add")
    devices = inv.load()
    taken = inv.handles(devices)
    if alias and alias in taken:
        err.print(f"[red]Another machine already answers to {alias!r}.[/red]")
        err.print("  [dim]aliases share the namespace with names, so the short form is "
                  "never ambiguous[/dim]")
        raise typer.Exit(2)
    if this_machine:
        # No ssh at all: `fleet add "ssh localhost"` would need inbound sshd on a laptop,
        # which is the thing run_probe_local exists to avoid.
        dev, res = onboard_self(name=name, kind=kind, alias=alias, taken_names=taken)
    else:
        ssh_command = _discovered_command(ssh_command)
        try:
            dev, res = onboard(ssh_command, name=name, kind=kind, alias=alias,
                               taken_names=taken)
        except ValueError as exc:
            # An address ssh itself refuses (`-o Port=abc`, a host that begins with
            # `-`) was a traceback; the words are the whole answer.
            if not _emit({"ok": False, "error": str(exc)}, json_out):
                err.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(2)
        if not json_out and (missing := _missing_key(ssh_command)):
            # ssh warns and carries on with its own keys; fleet carried on in silence, so
            # a mistyped key path was never seen, until those keys stopped being enough.
            err.print(f"[yellow]![/yellow] -i {escape(missing)}: no such file here, so ssh "
                      "offers its own keys instead")
        if res.status is Status.AUTH_FAILED and not json_out and not dry_run:
            dev, res = _offer_copy_id(ssh_command, dev, res, name=name, kind=kind,
                                      alias=alias, taken=taken)
    if res.status in _DID_NOT_ANSWER or res.status is Status.AUTH_FAILED:
        from .ops import doctor

        verdict = doctor.diagnose_result(dev, res, adding=True)
        if not _emit({"ok": False, "name": dev.name, "status": res.status.value,
                      "error": res.error_detail, "why": verdict.why, "fix": verdict.fix},
                     json_out):
            err.print(f"[red]{dev.name} {'refused us' if res.status is Status.AUTH_FAILED else 'did not answer'}"
                      f"[/red] — {res.status.value}: {escape(res.error_detail or '')}")
            err.print(f"  {escape(verdict.why)}")
            for line in verdict.fix:
                err.print(f"    [bold]{escape(line)}[/bold]")
            err.print("  [dim]nothing recorded; run the same `fleet add` again once it lets "
                      "you in[/dim]")
        raise typer.Exit(1)
    split = False
    if not this_machine:
        # Its note goes to stderr under --json: stdout is the one document an agent reads.
        with _chatter_to_stderr(json_out):
            split = _split_a_clone(devices, dev, res, write=not dry_run)
    if dry_run:
        # `name` and `action`, as the real add answers: agents read the same keys from
        # both. A machine already recorded keeps its name, so that is the one to report
        # -- `cloud-1-2`, the name a new machine would have got, named nothing.
        # A removed one too: looked up among the live alone, it was "added", and the
        # real add said "restored".
        known = None if split else next((d for d in devices if d.id == dev.id), None)
        if known is not None:
            new_eps = [e for e in dev.endpoints
                       if not any(inv.same_route(e, old) for old in known.endpoints)]
            action = ("restored" if known.deleted_at
                      else "endpoint_added" if new_eps else "unchanged")
            # What the real add would change on it, as that answers.
            if action == "unchanged" and ((kind and dev.kind is not known.kind) or any(
                    t not in known.tags for t in (_tags(tag) or [])) or (
                    alias and not known.alias and alias != known.name)):
                action = "updated"
        else:
            action = "added"
        shown = known.name if known else dev.name
        if known is not None and known.deleted_at and shown in inv.handles(
                devices, excluding=known.id):
            shown = dev.name        # as the real add renames it: its name is taken now
        _emit({"action": action, "name": shown, "id": dev.id,
               "kind": (dev.kind if kind or known is None else known.kind).value,
               "status": res.status.value, "dry_run": True,
               "clone_of_a_known_machine": split}, True)
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
        survivor = inv.find_exact(current, dev.id)
        was = (None if survivor is None or survivor is dev else
               (survivor.kind, list(survivor.tags), survivor.alias))
        # A new machine too: under `survivor is not dev` alone, its tags never reached
        # the list and `fleet ls --tag` found nothing.
        if survivor is not None and tags:
            apply_edits(survivor, add_tags=tags)
        if survivor is not None and survivor is not dev:
            # The same for what was said explicitly: `fleet add ... --kind rental` on a
            # machine removed earlier printed "added (rental)" and kept it permanent.
            if kind and survivor.kind is not dev.kind:
                # Back from shared or mobile, the policy that came with that kind went
                # too: a rental re-added from mobile kept `never`, and no `fleet ls`
                # looked at it again. One set by hand is left alone.
                came_with = {Kind.SHARED: "on_demand", Kind.MOBILE: "never"}
                if dev.kind in came_with or survivor.probe_policy == came_with.get(
                        survivor.kind):
                    survivor.probe_policy = dev.probe_policy
                survivor.kind = dev.kind
                inv.touch(survivor)
            elif dev.provider and survivor.kind is Kind.PERMANENT:
                # Unsaid, the probe decides as it would for a new machine: a box that
                # answers as a vast.ai instance is rented, whatever it was recorded as.
                survivor.kind = Kind.RENTAL
                inv.touch(survivor)
            if dev.provider and survivor.provider != dev.provider:
                survivor.provider = dev.provider
                inv.touch(survivor)
            if alias and not survivor.alias and alias != survivor.name:
                apply_edits(survivor, alias=alias)
        updated.clear()
        if action == "restored" and survivor is not None:
            # Its name given to another machine while it was removed: brought back under
            # it, two machines answered to one name, and every command refused the list.
            others = inv.handles(current, excluding=survivor.id)
            if survivor.name in others:
                old_name, n = survivor.name, 2
                fresh = dev.name if dev.name not in others else ""
                while not fresh:
                    fresh = f"{old_name}-{n}" if f"{old_name}-{n}" not in others else ""
                    n += 1
                survivor.name = fresh
                inv.touch(survivor)
                updated.append(f"name {old_name} -> {fresh}: another machine is called "
                               f"{old_name} now")
            if survivor.alias and survivor.alias in others:
                updated.append(f"alias {survivor.alias} -> (none): another machine "
                               "answers to it now")
                survivor.alias = ""
                inv.touch(survivor)
            if was is not None:
                was = (was[0], was[1], survivor.alias)
        if was is not None:
            # A machine already recorded, given a new --kind or --tag: it was changed,
            # and said "already recorded with this endpoint", action "unchanged".
            if survivor.kind is not was[0]:
                updated.append(f"kind {was[0].value} -> {survivor.kind.value}")
            if survivor.tags != was[1]:
                updated.append(f"tags {' '.join(was[1]) or '(none)'} -> "
                               f"{' '.join(survivor.tags) or '(none)'}")
            if survivor.alias != was[2]:
                updated.append(f"alias {was[2] or '(none)'} -> {survivor.alias}")
            if updated and action == "unchanged":
                action = "updated"
        return current, action

    updated: list[str] = []

    devices, action = inv.update(_record)
    # A machine already known keeps its own name: the record that survived is the one to
    # report. Reporting the requested name sent agents after a machine that did not exist
    # ("No device named 'loop'") on the very next command.
    asked = dev.name
    if survivor := inv.find_exact(devices, dev.id):
        # A restored machine too: it comes back under its old name.
        dev.name, dev.kind = survivor.name, survivor.kind
    for t in tags:
        if view_mod.is_fact_name(t):
            err.print(f"  [dim]note: {t!r} is also derived from telemetry — "
                      "kept, since a probe cannot see everything[/dim]")
    if res.snapshot is not None or not res.ok:
        conn = store.connect()
        store.record(conn, dev.id, res)
        conn.close()
    if not json_out:
        what = f"({dev.kind.value}, {res.status.value})"
        if action == "restored":
            console.print(f"[green]✓[/green] restored [bold]{dev.name}[/bold] {what} — it "
                          "had been removed, and everything recorded about it is back.")
        elif action == "endpoint_added":
            console.print(f"[green]✓[/green] {dev.name} {what} was already known — added "
                          "another endpoint (same machine-id, so this is one device, not two).")
        elif action == "unchanged":
            console.print(f"[dim]· {dev.name} {what} already recorded with this endpoint.[/dim]")
        elif action == "updated":
            console.print(f"[green]✓[/green] {dev.name} {what} was already recorded with "
                          "this endpoint")
        if action != "added":
            for line in updated:
                console.print(f"  {escape(line)}")
        if action != "added" and name and name != dev.name:
            # Offering a rename to a name another machine has sent people to a command
            # that only said no.
            if asked in inv.handles(devices, excluding=dev.id):
                console.print(f"  [dim]it keeps its name; another machine already answers "
                              f"to {asked!r}[/dim]")
            else:
                console.print(f"  [dim]it keeps its name; to rename it: [bold]fleet edit "
                              f"{dev.name} --name {asked}[/bold][/dim]")
        if action != "added" and alias and survivor and survivor.alias \
                and survivor.alias != alias:
            # Said as for --name: `--alias lb` on a machine already called `l` was dropped
            # without a word.
            console.print(f"  [dim]it keeps its alias {escape(survivor.alias)}; to change "
                          f"it: [bold]fleet edit {dev.name} --alias {escape(alias)}[/bold]"
                          "[/dim]")
        if action == "added":
            console.print(f"[green]✓[/green] added [bold]{dev.name}[/bold] {what}")
            if not name:
                # Said, because a guessed name is the one thing here nobody asked for,
                # and the hostname a cloud image ships with is rarely the one you meant.
                from .onboard import name_source

                eps = inv.endpoints_of(dev)
                source = name_source(res.snapshot, eps[0]) if eps else ""
                if source:
                    console.print(f"  [dim]named after {source}; to rename it: "
                                  f"[bold]fleet edit {dev.name} --name NEW[/bold][/dim]")
        if not res.ok:
            console.print(f"  [yellow]{res.status.value}[/yellow]: {escape(res.error_detail or '')}")
    _emit({"action": action, "name": dev.name, "id": dev.id, "kind": dev.kind.value,
           "status": res.status.value, **({"changes": updated} if updated else {})},
          json_out)


def _discovered_command(text: str) -> str:
    """A name from `fleet ls --discover` stands for the ssh command that reaches it."""
    from .ops import discover

    if text.split()[0] in ("ssh", "ssh.exe") or "@" in text or " " in text.strip():
        return text
    for cand in discover.candidates():
        if text in (cand.name, cand.target):
            return cand.ssh_command
    return text


def _offer_copy_id(ssh_command: str, dev, res, *, name, kind, alias, taken):
    """A machine that refused us, at a terminal: offer to put your key on it, once.

    `ssh-copy-id` asks for the password itself and puts your normal public key on the
    machine; fleet only starts it, never sees what is typed, and records nothing about
    it. Not offered without a terminal (an agent cannot type a password), nor where there
    is no ssh-copy-id (Windows): there `fleet doctor` prints the line to run.
    """
    from .ssh.keys import copy_id_argv, public_key
    from .ui import confirm

    eps = inv.endpoints_of(dev)
    if not eps or not sys.stdin.isatty():
        return dev, res
    # The key it is pinned to (`-i`), which ssh-copy-id reads from KEY.pub; else yours.
    if not (Path(eps[0].identity + ".pub").is_file() if eps[0].identity
            else public_key() is not None):
        return dev, res
    argv = copy_id_argv(eps[0])
    if argv is None:
        return dev, res
    err.print(f"[yellow]{dev.name} does not accept your key yet.[/yellow]")
    if not confirm(f"Put your key on it with `{' '.join(argv[1:])}`? "
                   "ssh-copy-id asks for its password once", default=True):
        return dev, res
    if subprocess.run(argv).returncode != 0:
        return dev, res
    return onboard(ssh_command, name=name, kind=kind, alias=alias, taken_names=taken)


def _usable_names(**given: str | None) -> None:
    """Refuse a name or alias that cannot be one (edit.check_name), before any work."""
    for what, value in given.items():
        if value:
            try:
                check_name(value, what)
            except ValueError as exc:
                err.print(f"[red]{escape(str(exc))}[/red]")
                raise typer.Exit(2)


def _split_a_clone(devices, dev, res, *, write: bool = True) -> bool:
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
    machine about which one it is -- rather than splitting it again on every add,
    or the clone believing it is the machine it was cloned from.
    """
    import secrets

    from .onboard import slugify

    existing = inv.find_exact(devices, dev.id)
    new = res.snapshot
    if existing is None or existing.id != dev.id or new is None:
        return False
    new_eps = inv.endpoints_of(dev)
    old_eps = sorted(inv.endpoints_of(existing), key=lambda e: e.preference)
    if new_eps and any((e.address, e.port) == (new_eps[0].address, new_eps[0].port)
                       for e in old_eps):
        return False                        # the same address: the same machine
    host = new.hostname or ""
    other, why = None, ""
    if old_eps:
        now = run_probe(old_eps[0], mode=existing.probe_mode, timeout=15)
        if now.snapshot is not None:
            if derive_id(now.snapshot, old_eps[0]) != dev.id:
                return False                # its old address is someone else now: moved
            other = now.snapshot
    if other is not None:
        booted = lambda s: (s.ts - s.uptime_s) if s.uptime_s is not None else None
        b_old, b_new = booted(other), booted(new)
        if other.hostname and host and other.hostname != host:
            why = f"calls itself {host!r}, not {other.hostname!r}"
        elif b_old is not None and b_new is not None and abs(b_old - b_new) > 120:
            why = "is running at the same time, booted at another moment"
        else:
            return False                    # one host, one boot: one machine, two routes
    else:
        conn = store.connect()
        try:
            _, snap = store.latest(conn, existing.id)
        finally:
            conn.close()
        before = (snap or {}).get("hostname") or ""
        if not before or not host or before == host:
            return False
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
    if not write:
        return True                         # --dry-run: nothing written anywhere
    from .ssh.keys import remote_device_id_command

    plat = remote_platform({"uname_s": new.uname_s, "os": new.os})
    ok, said = _run_script(new_eps[0], remote_device_id_command(new_id, host=host,
                                                                platform=plat),
                           platform=plat, capture=True) if new_eps else (False, "")
    if not (ok and "ok" in said):
        console.print("  [dim]its id could not be written on it, so it may be taken for "
                      f"{existing.name} again; giving it a machine-id of its own fixes "
                      "that: `systemd-machine-id-setup` (after emptying /etc/machine-id) "
                      "on it[/dim]")
    return True


def inv_unique(devices, wanted: str) -> str:
    taken, name, n = inv.handles(devices), wanted, 2
    while name in taken:
        name, n = f"{wanted}-{n}", n + 1
    return name


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
             cost: float = typer.Option(None, "--cost", metavar="USD",
                                        help="what it costs per hour, for the $/HR column "
                                             "and the fleet's burn rate; 0 clears it"),
             note: str = typer.Option(None, "--note", metavar="TEXT",
                                      help='a line about the machine; --note "" clears it'),
             path: list[str] = typer.Option(None, "--path", metavar="KIND=PATH",
                                            help="where this machine keeps models, datasets, "
                                                 "media...: models=/data/models; repeat a "
                                                 "kind for several, models= clears it"),
             endpoint: list[str] = typer.Option(None, "--endpoint", metavar="NAME=PORT[/PATH]",
                                                help="a model server fleet would not find by "
                                                     "itself: myllm=9000 or myllm=9000/v1; "
                                                     "myllm= removes it. Never a key"),
             local: bool = typer.Option(False, "--local",
                                        help="keep --tag/--untag/--cost in this machine's own "
                                             "list instead of on that machine"),
             json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
    """Change a device's address or settings after it was added.

    Rentals recycle IPs and ports, so `--ssh` re-points a device without losing its
    name, tags, cost or history. Tags, cost, a note, folders (`--path`) and declared
    servers (`--endpoint`) are written to that machine's own machine.yaml, so everyone
    who looks at it reads the same; `--local` keeps tags and cost here instead.

    [dim]Example:[/dim]  fleet edit machine_A --ssh "ssh -p 40001 root@5.6.7.8"
    """
    # The range every reader of a cost keeps. Outside it -- -5, nan, inf -- the machine's
    # file took it and printed ✓, and every probe after dropped it as invalid, so the
    # machine showed no cost at all and nothing said why.
    if cost is not None and not (0 <= cost < 1e6):
        err.print(f"[red]--cost {cost:g}[/red] is not a cost  "
                  "[dim]US dollars per hour, from 0 (which clears it) up to a million[/dim]")
        raise typer.Exit(2)
    if local and (note is not None or path or endpoint):
        # --local is for a machine you would rather not write to; a note, a folder or a
        # server has nowhere else to go, so it went onto the machine all the same.
        what = ", ".join(o for o, v in (("--note", note is not None), ("--path", path),
                                        ("--endpoint", endpoint)) if v)
        err.print(f"[red]--local keeps only tags and cost here[/red]  [dim]{what} is kept "
                  "only on the machine itself: drop --local to write it there[/dim]")
        raise typer.Exit(2)
    if ssh_command is not None and not ssh_command.strip():
        # `--ssh ""` said "nothing to change", as though the address had been kept.
        err.print("[red]--ssh needs how you connect to it[/red]  [dim]e.g. --ssh "
                  "\"ssh -p 40001 root@5.6.7.8\"[/dim]")
        raise typer.Exit(2)
    # Before anything is written: a bad --ssh refused after the tags had gone onto the
    # machine left half the edit done.
    try:
        ssh_to = resolve_command(ssh_command) if ssh_command is not None else None
    except ValueError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    devices = inv.load()
    dev = _this_machine(devices, "edit") if name is None else inv.find(devices, name)
    if dev is None:
        _no_such(devices, name)

    on_machine = bool(path or endpoint or note is not None
                      or (not local and (tag or untag or cost is not None)))
    on_machine_doc: dict | None = None
    more = True
    if on_machine:
        here_tag, here_untag, here_cost = tag, untag, cost
        if not local:
            here_tag = None
            # `fleet add --tag` keeps its tags in this list, so removing one has to
            # reach here as well: otherwise it said "tags: -" and `ls` still showed it.
            here_untag = [t for t in (_tags(untag, untag=True) or []) if t in dev.tags] or None
            # The same for a cost kept here with --local: `--cost 0` said "cleared" and
            # the machine went on costing that much in `ls`.
            here_cost = 0 if cost == 0 and (dev.cost or {}).get("usd_per_hour") else None
        # `here_cost` by itself: 0 is the one cost that asks for something, and
        # `0 in (None, False)` is true.
        more = here_cost is not None or any(v not in (None, False, []) for v in (
            ssh_command, disk_path, clear_disk_paths, new_name, new_alias, here_tag,
            here_untag))
        machine_tags = ([] if local else (_tags(tag) or []),
                        [] if local else (_tags(untag, untag=True) or []))
        machine_cost = None if local else cost
        tag, untag, cost = here_tag, here_untag, here_cost

    # Both at once: start again from these. Clearing first dropped the new paths too.
    paths = list(disk_path) if disk_path else ([] if clear_disk_paths else None)
    old_name, dev_id = dev.name, dev.id

    def _edit(target, current):
        return apply_edits(target, name=new_name, alias=new_alias,
                           add_tags=_tags(tag), drop_tags=_tags(untag, untag=True),
                           taken=inv.handles(current, excluding=target.id),
                           endpoint=ssh_to, disk_paths=paths, usd_per_hour=cost)

    result = None
    if more:
        try:
            # A dry run on a copy, to refuse or report "nothing to change" before taking
            # the lock -- and before the machine's own file is written: a name already
            # taken, or an --ssh that reaches another machine, said "nothing changed"
            # after the tags had gone onto the machine.
            result = _edit(copy.deepcopy(dev), devices)
        except ValueError as exc:
            # apply_edits refuses in words -- a clashing alias, a name already taken --
            # and those words are the whole answer. A traceback around them was not.
            err.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(2)
        if (result.changes and ssh_to is not None
                and (other := _answers_as_another(dev, ssh_to, devices))):
            err.print(f"[red]{escape(ssh_command)} reaches {escape(other)}, not "
                      f"{dev.name}[/red]  [dim]-- nothing changed. A typo, or the address "
                      "now belongs to another machine; `fleet add` it if it is a new "
                      "one.[/dim]")
            raise typer.Exit(2)
    if on_machine:
        # One --json document for the whole edit: two, back to back, are not JSON, and
        # the MCP server handed an agent the raw text instead of the result.
        on_machine_doc = _edit_machine_file(
            dev, json_out, paths=path or [], endpoints=endpoint or [], note=note,
            tags_add=machine_tags[0], tags_remove=machine_tags[1], cost=machine_cost,
            emit=not more)
        if not more:
            return
    if not result.changes:
        # `--untag g4 --local` with g4 in the machine's own file said "nothing to
        # change", and `ls` went on showing g4 with no word of where it came from.
        theirs_tags: list[str] = []
        if local and untag:
            conn = store.connect()
            _, snap = store.latest(conn, dev.id)
            conn.close()
            on_file = ((snap or {}).get("notes") or {}).get("tags") or []
            theirs_tags = [t for t in (_tags(untag, untag=True) or []) if t in on_file]
        if _emit({**(on_machine_doc or {}), "name": dev.name, "changes": [],
                  **({"kept_on_machine": theirs_tags} if theirs_tags else {})}, json_out):
            return
        if theirs_tags:
            console.print(f"[yellow]· {dev.name}'s own machine.yaml has "
                          f"{escape(', '.join(theirs_tags))}, and --local cannot take it "
                          "off[/yellow]  [dim]drop --local to remove it there[/dim]")
        elif local and untag and not (tag or cost is not None or new_name or new_alias
                                      or ssh_command or disk_path or clear_disk_paths):
            console.print(f"[dim]· {dev.name} has no tag "
                          f"{escape(', '.join(_tags(untag, untag=True) or []))} here.[/dim]")
        else:
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
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    if result.previous_id:
        # the cache is keyed by id; without this the device looks brand new
        conn = store.connect()
        store.rename_device(conn, result.previous_id, dev.id)
        conn.close()
    if any(c.startswith("disk paths ") for c in result.changes):
        # The reading cached a minute ago measured the old mounts: `fleet ls` straight
        # after `--disk-path /data` showed every disk but /data, as if it were ignored.
        conn = store.connect()
        store.expire(conn, dev.id)
        conn.close()

    # A cost kept here is only the fallback: the machine's own file wins. "cost none ->
    # $0.5/hr" with $1.75 still in `ls` read as an edit that had not taken.
    theirs = None
    if local and cost and any(c.startswith("cost ") for c in result.changes):
        conn = store.connect()
        state, snap = store.latest(conn, dev.id)
        conn.close()
        if state and not state.get("last_probe_at"):
            # fleet changed the machine's own file since that reading (`fleet edit NAME
            # --cost 1` a moment ago): it knew nothing of that cost, and "cost none ->
            # $3/hr" came back while `ls` showed $1. It answered then, so ask it again.
            _rows([dev.name], refresh=False, rescan=False, detail=Detail.COMPACT)
            conn = store.connect()
            _, snap = store.latest(conn, dev.id)
            conn.close()
        theirs = (((snap or {}).get("notes") or {}).get("cost") or {}).get("usd_per_hour")
    if _emit({**(on_machine_doc or {}), "name": dev.name, "id": dev.id,
              "changes": result.changes,
              **({"cost_in_effect": theirs} if theirs is not None else {})}, json_out):
        return
    console.print(f"[green]✓[/green] {dev.name}")
    for line in result.changes:
        console.print(f"  {escape(line)}")
    if theirs is not None:
        console.print(f"  [yellow]its own machine.yaml says ${theirs:g}/hr, and that wins[/yellow]  "
                      "[dim]drop --local to change it there[/dim]")
    if ssh_to is not None:
        console.print(f"  [dim]run `fleet ls {dev.name} -r` to confirm it answers.[/dim]")


def _answers_as_another(dev: Device, endpoint, devices) -> str | None:
    """What a new address for `dev` reaches, when that is plainly another machine.

    `edit --ssh` took any address on trust, and every later reading of whatever answered
    there was recorded as this machine: a port typo pointed `gpubox` at the laptop, and
    `fleet ls` showed gpubox with no GPUs, `fleet ssh gpubox` opened the laptop. Only a
    machine that answers with a machine-id of its own is judged; one that does not
    answer may just be down, and an address-keyed device has no id to compare.
    """
    if dev.id.startswith("net:"):
        return None
    res = run_probe(endpoint, mode=dev.probe_mode, timeout=15, multiplex=False)
    if res.snapshot is None or not res.snapshot.machine_id:
        return None
    seen = derive_id(res.snapshot, endpoint)
    if seen == dev.id:
        return None
    known = inv.find_exact(devices, seen)
    return known.name if known else f"another machine ({res.snapshot.hostname or seen})"


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


def _routes(dev) -> list:
    """The device's routes, the one that answered its last probe first. By preference
    alone, install, update and edit went by a route that was down and failed on a
    machine `fleet ls` showed up -- reached the other way."""
    conn = store.connect()
    try:
        state, _ = store.latest(conn, dev.id)
    finally:
        conn.close()
    return inv.by_use(dev, state)


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
                forward_agent: bool = typer.Option(None, "--forward-agent/--no-forward-agent",
                                                   help="authenticate the clone as you, "
                                                        "leaving no credential on the device; "
                                                        "by default only for a git URL "
                                                        "over ssh")):
    """Install fleet on a device, so you and its agents can use fleet from there too.

    A machine fleet only looks at needs nothing installed; this is for one you also work
    on. It installs from git; re-running updates an existing install.

    [dim]Example:[/dim]  fleet install machine_A
    """
    if name is None:
        # This machine is never an ssh target -- its record has no address -- so
        # "install here" could only fail with "no endpoint recorded".
        err.print("[red]Name the machine to install fleet on.[/red] [dim]This one already "
                  "runs fleet: [bold]fleet update[/bold] updates it.[/dim]")
        raise typer.Exit(2)
    devices = inv.load()
    dev = inv.find(devices, name)
    if dev is None:
        _no_such(devices, name)
    eps = inv.endpoints_of(dev)
    if not eps:
        if dev.id and dev.id == identity.local_device_id():
            # Named, this machine said "has no endpoint recorded", as if it were broken.
            err.print(f"[red]{dev.name} is this machine[/red], which already runs fleet  "
                      "[dim]fleet update updates it[/dim]")
        else:
            err.print(f"[red]{dev.name} has no endpoint recorded[/red] [dim]-- give it "
                      f"one: fleet edit {dev.name} --ssh \"ssh user@host\"[/dim]")
        raise typer.Exit(1)

    url = repo or configured_repo()
    if not url:
        err.print("[red]No repo to install from.[/red]  Pass [bold]--repo "
                  "git@github.com:you/fleet.git[/bold], or set [bold]repo:[/bold] in "
                  f"{INVENTORY_PATH.parent / 'config.yaml'}")
        raise typer.Exit(2)

    platform = _remote_platform_of(dev)

    console.print(f"[dim]installing fleet on {dev.name} from {url} ({ref})[/dim]")
    from_git = bool(repo) or ref != "main"
    if forward_agent is None:
        # A fresh install clones from `url`; one that finds fleet from PyPI upgrades it
        # there and clones nothing, but that is only known on the far side.
        forward_agent = needs_agent(url)
    code, output = run_installer(_routes(dev)[0],
                                 install_script(url, ref=ref, platform=platform,
                                                from_git=from_git),
                                 forward_agent=forward_agent, platform=platform)
    if code != 0:
        err.print(f"[red]Install failed[/red] (exit {code})\n{escape(output.strip()[-600:])}")
        if code == 90:
            err.print("  [dim]the device could not fetch uv -- it may have no outbound "
                      "internet, or no curl.[/dim]")
        raise typer.Exit(2)

    if kept := _kept(output):
        # A source checkout there is kept, as `fleet update` says: "✓ node: fleet 0.6.1"
        # under "installing fleet ... from <url> (main)" read as main installed over it.
        console.print(f"[dim]·[/dim] {dev.name}: {escape(kept)}")
        return
    what = output.strip().splitlines()[-1] if output.strip() else "installed"
    console.print(f"[green]✓[/green] {dev.name}: {what}")
    console.print(f"  [dim]fleet there looks at the machines its own ssh reaches; copy "
                  f"{INVENTORY_PATH.name} there for the ones you added here by hand[/dim]")


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


def _kept(output: str) -> str:
    """What the installer said when it left fleet as it was, or "".

    It keeps a source checkout, or a fleet installed some other way, and says so -- and
    exits 0, since nothing failed. That line was dropped and a "✓" printed in its place,
    which reads as updated: the one thing that had not happened.
    """
    for line in output.splitlines():
        if line.startswith("fleet here ") and ("kept as it is" in line
                                               or "NOT updated" in line):
            return line.strip()
    return ""


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
            _no_such(devices, name)
        targets = [one]
    else:
        targets = []

    me = identity.local_device_id()
    ok = failed = skipped = 0

    # This machine first and without ssh. It is never an ssh target of its own, so
    # connecting to ourselves would fail on exactly the machine most likely to be
    # running the command.
    here = not name or (targets and any(d.id == me for d in targets))
    # On Windows this machine goes last, in the background, once this process has
    # exited: a running fleet holds the very files the update replaces.
    here_later = here and local_platform() == WINDOWS
    if here and not here_later:
        # The URL only when git was asked for: a PyPI install is upgraded from PyPI,
        # and "from https://github.com/... (main)" said otherwise.
        console.print("[dim]updating this machine"
                      + (f" from {url} ({ref})" if from_git else "") + "[/dim]")
        # local_platform, not `sh -c`: on Windows that shell is git's, and the
        # POSIX script half-runs under it. This is the machine running the command, so
        # it certainly has fleet -- update_only would be true either way, and saying so
        # keeps one script shape for every path.
        local_script = install_script(url, ref=ref, platform=local_platform(),
                                      update_only=True, from_git=from_git)
        p = subprocess.run(local_install_argv(),
                           input=payload_for(local_script, local_platform()),
                           capture_output=True)
        if p.returncode == 0 and (kept := _kept(p.stdout.decode(errors="replace"))):
            console.print(f"[dim]·[/dim] this machine: {escape(kept)}")
            skipped += 1
        elif p.returncode == 0:
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
        eps = _routes(dev)
        # Per device, because a fleet is not one platform. Building the script once and
        # sending it to everything handed the POSIX installer to a Windows machine, which
        # runs it under git's sh.exe far enough to break the installation it was meant
        # to update -- `fleet install` had always read the platform, and this had not.
        platform = _remote_platform_of(dev)
        console.print(f"[dim]updating {dev.name}[/dim]")
        code, output = run_installer(eps[0],
                                     install_script(url, ref=ref, platform=platform,
                                                    update_only=True, from_git=from_git),
                                     forward_agent=needs_agent(url), platform=platform)
        if code == 0 and (kept := _kept(output)):
            console.print(f"[dim]·[/dim] {dev.name}: {escape(kept)}")
            skipped += 1
        elif code == 0:
            # Which version it is on now: a bare tick did not say whether anything
            # changed, and that is the one thing an update is asked.
            found = re.findall(r"\bfleet (\d+\.\d+[\w.+-]*)", output)
            console.print(f"[green]✓[/green] {dev.name}"
                          + (f"  [dim]fleet {escape(found[-1])}[/dim]" if found else ""))
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
                      f"[dim]{escape(output.strip()[-120:])}[/dim]")
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


def _refuse_unknown(names, json_out: bool) -> None:
    """Fail on a name that matches no machine, with the ones it may have meant: `fleet ls
    v`, a prefix of both vm and vm-2, said only "no machine named 'v'" where `ssh`, `edit`
    and `rm` name the two."""
    devices = inv.load()
    unknown = [n for n in (names or []) if inv.find(devices, n) is None]
    if not unknown:
        return
    near = {n: m for n in unknown if (m := inv.near_matches(devices, n))}
    if not _emit({"ok": False, "error": f"no machine named {', '.join(unknown)}",
                  "unknown": unknown, **({"did_you_mean": near} if near else {})}, json_out):
        err.print(f"[red]No machine named {', '.join(repr(n) for n in unknown)}[/red] "
                  "[dim]-- `fleet ls` lists them all[/dim]")
        for n, m in near.items():
            err.print(f"  [dim]{escape(n)}: did you mean {escape(', '.join(m))}[/dim]"
                      if len(near) > 1 or len(unknown) > 1
                      else f"  [dim]did you mean: {escape(', '.join(m))}[/dim]")
    raise typer.Exit(1)


def _named_rows(names: list[str] | None, json_out: bool, *, refresh: bool,
                detail: Detail, rescan: bool = False, wait: int = 0) -> list[dict]:
    """The rows for `ps` and `models`: every machine, or the named ones, as `ls` gets them."""
    _first_run("ls")
    _refuse_unknown(names, json_out)
    return _rescanned(list(names) if names else None, refresh=refresh, rescan=rescan,
                      wait=wait, detail=detail)


@app.command("ps")
def cmd_ps(names: list[str] = typer.Argument(None, help="only these machines"),
           json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
           refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe")):
    """What is running on every GPU, across the fleet, and which GPUs are idle.

    [dim]Example:[/dim]  fleet ps --json
    """
    rows = _named_rows(names, json_out, refresh=refresh, detail=Detail.FULL)
    view = view_mod.jobs_view(rows)
    if _emit(view, json_out):
        return
    for part in render_ps(view):
        console.print(part)


@app.command("models")
def cmd_models(names: list[str] = typer.Argument(None, help="only these machines"),
               json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents"),
               refresh: bool = typer.Option(False, "--refresh", "-r", help="force a live probe"),
               rescan: bool = typer.Option(False, "--rescan", help="also re-measure the model folders, which machines otherwise cache for 6 hours"),
               wait: int = typer.Option(0, "--wait", metavar="SECONDS",
                                        help="with --rescan: wait up to this long for the folder sizes it started measuring")):
    """Every model being served across the fleet: which server, where, and how to reach it.

    [dim]Example:[/dim]  fleet models --json
    """
    rows = _named_rows(names, json_out, refresh=refresh, detail=Detail.FULL, rescan=rescan,
                       wait=wait)
    view = view_mod.models_view(rows)
    if _emit(view, json_out):
        return
    for part in render_models(view):
        console.print(part)


@app.command("top")
def cmd_top(name: str = typer.Argument(None, help="one device, instead of the whole fleet"),
            interval: float = typer.Option(2.0, "--interval", "-i",
                                           help="seconds between refreshes")):
    """Live view of the fleet, or of one device. Like htop, for your machines.

    Anything unreachable backs off instead of being redialled every couple of seconds.
    Shared multi-user hosts are probed only when asked for by name (`fleet ls NAME`).

    [dim]Example:[/dim]  fleet top machine_A -i 1
    """
    # nan and inf passed the test below and came back as a traceback from the first
    # schedule computed with them.
    if not math.isfinite(interval) or interval <= 0:
        err.print("[red]--interval must be more than 0 seconds[/red]")
        raise typer.Exit(2)
    _first_run("top")
    cfg = load_config()
    seen = inv.stamp()
    devices = inv.live(inv.load())
    if name:
        one = inv.find(devices, name)
        if one is None:
            _no_such(devices, name)
        devices = [one]

    schedule = Schedule(interval=interval,
                        shared_interval=float(cfg.shared_min_interval_s))
    conn = store.connect()
    detail = Detail.FULL
    live_within = max(int(interval * 3), 5)

    # No keys to press when they cannot be read: stdin is not this terminal.
    def frame(keys: bool = sys.stdin.isatty(), probe: bool = True, live: bool = True):
        rows = (_live_tick(conn, devices, schedule, cfg, detail, named=bool(name)) if probe
                else rows_mod.render(conn, devices, detail))
        if name:
            # The fleet view said how to leave; one machine's said nothing at all.
            return Group(render_device(rows[0], live_within),
                         *([Text.from_markup("\n[dim]q quit   r refresh[/dim]")] if keys
                           else []))
        view = fleet_view(rows)
        shown = _rows_that_fit(rows, console.height - 3) if live else rows
        table = render_fleet(shown, view["summary"], live_within, width=console.width)
        s = view["summary"]
        more = len(rows) - len(shown)
        return Group(table, Text.from_markup(
            f"\n[dim]{s['online']}/{s['total']} online · {s['gpus_free']} free GPU(s)"
            + (f" · {view_mod.usd(s['hourly_burn'])}/hr" if s["hourly_burn"] else "")
            + (f" · {more} more machine(s) below: make the window taller" if more else "")
            + ("  ·  q quit   r refresh" if keys else "") + "[/dim]"))

    # A live loop in a pipe would spin forever, and an agent is exactly what would run
    # it that way. One frame is also the more useful thing for a script -- and no keys
    # to press: "q quit  r refresh" under a frame that had already ended.
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        console.print(frame(keys=False, live=False))
        if sys.stdout.isatty():
            # A terminal that cannot move the cursor: the live view drew nothing at all
            # and said nothing about why, so one frame and the reason instead.
            console.print("[dim]this terminal (TERM=dumb) cannot redraw in place, so "
                          "this is one frame -- set TERM, e.g. xterm-256color, for the "
                          "live view[/dim]")
        conn.close()
        return

    # The window draws what the store has while the machines are asked in the
    # background, so it is there at once and `q` always answers.
    probes = rows_mod.Background(devices, lambda: Schedule(
        interval=interval, shared_interval=float(cfg.shared_min_interval_s)),
        cfg, named=bool(name)).start()
    try:
        with _raw_stdin(), Live(frame(probe=False), console=console, screen=True,
                                refresh_per_second=8) as live:
            while True:
                # At least once a second, so a machine that just answered shows up.
                keys = _key_pressed(min(interval, 1.0))
                if any(k in keys for k in ("q", "Q", "\x03", "\x04")):
                    break
                if "r" in keys or "R" in keys:
                    probes.again()
                # Another terminal or an agent may add, rename or remove a machine
                # while this is open: the window went on showing a removed one online,
                # and costing, until it was restarted.
                fresh, seen = _list_now(devices, seen, one=devices[0].id if name else None)
                if fresh is not devices:
                    devices = fresh
                    probes.follow(devices)
                live.update(frame(probe=False))
    except KeyboardInterrupt:
        pass
    finally:
        probes.stop()
        conn.close()


def _list_now(devices: list, seen, *, one: str | None = None):
    """The machine list again if any `fleet` wrote it since `seen`, else `devices`.

    `one` follows a single machine by id, so a rename shows; a list that cannot be read
    just now, or no longer has that machine, leaves what is on screen as it is.
    """
    now = inv.stamp()
    if now == seen:
        return devices, seen
    try:
        fresh = inv.live(inv.load())
    except inv.InventoryError:
        return devices, now
    if one is not None:
        fresh = [d for d in fresh if d.id == one] or devices
    return fresh, now


def _rows_that_fit(rows: list[dict], lines: int) -> list[dict]:
    """The machines whose rows fit in `lines` of the window, below the table's header.

    The window is cut at its bottom edge: on a terminal shorter than the fleet the rest
    of it, and the line that counts what is online and says how to quit, were cut off
    without a word.
    """
    shown, used = [], 1
    for row in rows:
        used += device_lines(row)
        if used > lines and shown:
            break
        shown.append(row)
    return shown


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


def _key_pressed(timeout: float) -> str:
    """Doubles as the frame delay: waits for a key, or returns when the interval is up.

    Every key waiting, not one: `rq` typed together left the q in sys.stdin's buffer,
    where select() never saw it, and `fleet top` did not quit. And with no terminal to
    read keys from (`fleet top < /dev/null`) it still waits: returning at once spun the
    loop at a whole core.
    """
    import select as _select
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return ""
    if sys.platform == "win32":
        # select() takes only sockets on Windows; the console is polled instead.
        import msvcrt

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                keys = ""
                while msvcrt.kbhit():
                    keys += msvcrt.getwch()
                return keys
            time.sleep(0.05)
        return ""
    fd = sys.stdin.fileno()
    if _select.select([fd], [], [], timeout)[0]:
        # Nothing at all from a ready terminal is one that hung up: quit, not spin.
        return os.read(fd, 1024).decode(errors="replace") or "\x04"
    return ""


@app.command("rm")
def cmd_rm(name: str = typer.Argument(..., help="the exact name; a prefix is never enough"),
           yes: bool = typer.Option(False, "--yes", "-y", help="do not ask for confirmation")):
    """Stop watching a machine: take it out of this machine's list.

    Nothing is changed on the machine itself: what `fleet edit` wrote to its
    machine.yaml (tags, cost, a note, folders, servers), and fleet if you installed it
    there, stay with it.

    [dim]Example:[/dim]  fleet rm machine_A
    """
    devices = inv.load()
    # Exact only: a prefix someone half-remembered must never pick the machine.
    dev = inv.find_exact(devices, name)
    if dev is None:
        err.print(f"[red]No machine named exactly {name!r}[/red]")
        if near := inv.near_matches(devices, name):
            err.print(f"  [dim]did you mean: {', '.join(near)}[/dim]")
        raise typer.Exit(1)
    if not yes and not sys.stdin.isatty():
        # The prompt read nothing and said only "Aborted.", as `fleet uninstall` once did.
        err.print(f"[red]Removing {dev.name} needs a yes: run it in a terminal, or "
                  "pass --yes.[/red]")
        raise typer.Exit(2)
    if not yes and not typer.confirm(f"Remove {dev.name} ({dev.kind.value}) from the list?"):
        raise typer.Exit(1)

    def _forget(current):
        inv.remove(current, inv.find_exact(current, dev.id))
        return current, None

    inv.update(_forget)
    conn = store.connect()
    try:
        store.forget_except(conn, {d.id for d in inv.live(inv.load())} or {"-"})
    finally:
        conn.close()
    console.print(f"[green]✓[/green] removed {dev.name}  [dim](add it back any time with "
                  "fleet add)[/dim]")


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
    devices = inv.load()
    dev = inv.find(devices, name)
    if dev is None:
        _no_such(devices, name)
    if raw:
        argv = build_argv(_routes(dev)[0],
                          remote="sh -s", env=probe_env(dev.probe_mode, dev.disk_paths))
        p = sshrun(argv, input=PAYLOAD.read_bytes())
        sys.stdout.write(p.stdout.decode(errors="replace"))
        raise typer.Exit(0 if p.returncode == 0 else 1)
    res = run_probe(_routes(dev)[0], mode=dev.probe_mode,
                    disk_paths=dev.disk_paths)
    console.print_json(jsonlib.dumps(
        {"status": res.status.value, "latency_ms": res.latency_ms,
         "error": res.error_detail, "snapshot": res.snapshot.to_dict() if res.snapshot else None},
        default=str))


@app.command("ssh", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def cmd_ssh(ctx: typer.Context,
            name: str = typer.Argument(..., help="a machine's name or alias")):
    """Open a shell on a device, or run a command: `fleet ssh machine_A -- nvidia-smi`.

    The wrapper resolves the endpoint and connects exactly as you would, so an agent
    never needs an address, a key or a password -- only the machine's name.

    [dim]Example:[/dim]  fleet ssh machine_A -- nvidia-smi
    """
    devices = inv.load()
    dev = inv.find(devices, name)
    if dev is None:
        _no_such(devices, name)
    conn = store.connect()
    try:
        cached, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    eps = inv.by_use(dev, cached)
    if not eps:
        if dev.id == identity.local_device_id():
            err.print(f"[red]{dev.name} is this machine[/red] -- run the command here, "
                      "without fleet ssh")
        else:
            err.print(f"[red]{dev.name} has no endpoint recorded[/red] [dim]-- give it "
                      f"one: fleet edit {dev.name} --ssh \"ssh user@host\"[/dim]")
        raise typer.Exit(1)
    ep = eps[0]
    platform = remote_platform(snap)
    # A cached refusal is evidence only while it is fresh: you may have fixed it since,
    # and nothing tells fleet. Stale, we try -- a key that really is missing fails in
    # under a second with ssh's own message.
    fresh_s = int(load_config().get("telemetry_ttl_s") or 0)
    probed_at = int((cached or {}).get("last_probe_at") or 0)
    if auth_of(dev, cached) == "refused" and time.time() - probed_at <= fresh_s:
        err.print(f"[yellow]{dev.name} refused us a moment ago.[/yellow] "
                  f"[bold]fleet doctor {dev.name}[/bold] says why, and how to let us in.")
        raise typer.Exit(2)
    # accept-new, as the probe has always used: an unknown host key is recorded, a
    # changed one is still refused -- an agent has no terminal to answer ssh's question.
    # Keys only, in batch: fleet never handles a password, and a prompt here reads as
    # though typing one would work for the agent that asked.
    opts = ["-o", "StrictHostKeyChecking=accept-new",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            # A port that takes the connection and never speaks ssh held `fleet ssh`, and
            # the agent's run_on_machine, until its own limit, which then said the
            # command "may still be running" when it had never started.
            "-o", f"ConnectTimeout={int(load_config().get('connect_timeout_s') or 8)}"]
    argv = ["ssh", *opts]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.identity:
        # As the probe does: a machine recorded with a key of its own is offered that key
        # alone. Without it the agent's keys went first, and a rental with MaxAuthTries
        # 3 answered `fleet ssh` "Too many authentication failures" while `fleet ls`,
        # pinned to the key, reached it fine.
        argv += ["-o", "IdentitiesOnly=yes", "-i", ep.identity]
    if ep.jump:
        argv += jump_args(ep.jump, opts)
    argv += ["--", f"{ep.user}@{ep.target}" if ep.user else ep.target]
    # click has already taken the separator. Any `--` left is the command's own, and
    # dropping it ran `fleet ssh box -- git log -- README.md` as `git log README.md`.
    extra = list(ctx.args)
    if extra:
        from .ssh.cmd import expanded_here

        if hint := expanded_here(extra, home=str(Path.home()),
                                 here_windows=local_platform() == WINDOWS,
                                 there_windows=platform == "windows"):
            err.print(f"[yellow]![/yellow] {hint}")
        # Your config's RemoteCommand is for a shell; with a command of its own ssh
        # refused both ("Cannot execute command-line and remote command").
        argv[1:1] = ["-o", "RemoteCommand=none"]
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


# A machine that answers one of these is awake, whatever else it says: a refused key is
# for `fleet doctor`, not a reason to keep waiting. `refused` is not one -- the port is
# closed while it boots.
_AWAKE = (Status.OK.value, Status.AUTH_FAILED.value, Status.HOST_KEY_MISMATCH.value,
          Status.PROBE_ERROR.value)


@app.command("wake")
def cmd_wake(name: str = typer.Argument(..., help="a machine's name or alias"),
             via: str = typer.Option(None, "--via", metavar="NAME",
                                     help="send the packet from this machine, which must "
                                          "be on the same network"),
             no_wait: bool = typer.Option(False, "--no-wait",
                                          help="send the packet and return, without "
                                               "waiting for it to answer"),
             timeout: int = typer.Option(180, "--timeout", metavar="SECONDS",
                                         help="how long to wait for it to answer"),
             json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
    """Wake a sleeping or switched-off machine with a Wake-on-LAN packet, and wait for it.

    The packet is a broadcast on the machine's own network. It is sent from here when
    this machine is on that network, and otherwise by a fleet machine that is, over ssh
    -- so from anywhere, while one machine there is up. What the machine's cards are,
    and whether each is armed to wake, its last probe read; its machine.yaml can name
    the card instead (`wake: {mac: ...}`).

    [dim]Example:[/dim]  fleet wake gpu-tower
    """
    from .ops import wake

    devices = inv.live(inv.load())
    dev = inv.find(devices, name)
    if dev is None:
        _no_such(devices, name)
    me = identity.local_device_id()

    def fail(message: str, fix: list[str] = (), *, code: int = 2, **extra):
        if not _emit({"ok": False, "name": dev.name, "error": message, "fix": list(fix),
                      **extra}, json_out):
            err.print(f"[red]{escape(message)}[/red]")
            for line in fix:
                err.print(f"  [dim]{escape(line)}[/dim]", soft_wrap=True)
        raise typer.Exit(code)

    if me and dev.id == me:
        fail(f"{dev.name} is this machine, and it is awake")
    if dev.kind in wake.NEVER:
        fail(f"{dev.name} is a {dev.kind.value} machine: Wake-on-LAN wakes your own "
             "hardware, on your own network",
             ["start it from the provider's console or command line"])
    via_dev = None
    if via is not None:
        via_dev = inv.find(devices, via)
        if via_dev is None:
            _no_such(devices, via)
        if via_dev.id == dev.id:
            fail(f"{dev.name} cannot wake itself: --via names a machine that is up")
    conn = store.connect()
    try:
        state, snap = store.latest(conn, dev.id)
        others = [(d, *store.latest(conn, d.id)) for d in devices if d.id not in (dev.id, me)]
    finally:
        conn.close()
    status = (state or {}).get("status") or ""
    if status == Status.OK.value and store.is_fresh(state, int(load_config().telemetry_ttl_s)):
        if not _emit({"ok": True, "name": dev.name, "sent": False, "woke": False,
                      "status": status, "already_up": True}, json_out):
            console.print(f"[green]✓[/green] {dev.name} is up [dim]-- it answered "
                          f"{_ago(int(time.time()) - int(state.get('last_probe_at') or 0))} ago"
                          "; nothing sent[/dim]")
        return

    age = int(time.time() - snap["ts"]) if snap and snap.get("ts") else None
    seen = f" (what fleet knows of its network is {_ago(age)} old)" if age and age > 86400 else ""
    macs = wake.target_macs(snap)
    if not macs:
        if (snap or {}).get("nics"):
            fail(f"every network card of {dev.name} said Wake-on-LAN is off when it was "
                 "last probed",
                 ["turn it on in its BIOS/UEFI (\"Wake on LAN\", \"Power on by PCI-E\"), then:",
                  "Linux:   sudo ethtool -s IFACE wol g   (and keep it, e.g. NetworkManager's "
                  "802-3-ethernet.wake-on-lan magic)",
                  "macOS:   System Settings → Battery/Energy → Wake for network access",
                  "Windows: Device Manager → the adapter → Power Management → "
                  "Only allow a magic packet to wake the computer",
                  "or name the card in its machine.yaml: wake: {mac: aa:bb:cc:dd:ee:ff}"],
                 facts_age_s=age)
        fail(f"fleet does not know {dev.name}'s network card",
             [f"once it is awake, `fleet ls -r {dev.name}` reads it",
              "or name the card in its machine.yaml: wake: {mac: aa:bb:cc:dd:ee:ff}"],
             facts_age_s=age)

    here = next((d for d in devices if me and d.id == me), None)
    here_name = here.name if here else "this machine"
    if via_dev is not None and me and via_dev.id == me:
        senders = [wake.Sender(here_name, None, "via")]
    else:
        here_snap = None
        if via_dev is None:
            res = run_probe_local(light=True)
            here_snap = res.snapshot.to_dict() if res.ok and res.snapshot else None
        senders = wake.choose_senders(dev, snap, here_snap, others, here_name=here_name,
                                      via=via_dev)
    if not senders:
        fail(f"no machine on {dev.name}'s network is up to send the packet{seen}",
             ["wake one there by hand, or name one that is up:  "
              f"fleet wake {dev.name} --via NAME"], facts_age_s=age)

    ip, prefix = wake.subnet_of(snap)
    broadcast = wake.broadcast_for(ip, prefix)
    tried, used = [], None
    for s in senders:
        if s.device is None:
            ok, why = wake.send(s, macs, broadcast)
        else:
            routes = _routes(s.device)
            ok, why = wake.send(s, macs, broadcast, endpoint=routes[0] if routes else None,
                                platform=_remote_platform_of(s.device))
        tried.append({"name": s.name, "how": s.how, "ok": ok, **({"error": why} if why else {})})
        if ok:
            used = s
            break
    result = {"name": dev.name, "macs": macs, "broadcast": broadcast,
              "via": used.name if used else None, "how": used.how if used else None,
              "tried": tried, "facts_age_s": age}
    if used is None:
        fail("the packet could not be sent", [f"{t['name']}: {t.get('error', '')}" for t in tried],
             code=1, **{k: v for k, v in result.items() if k != "name"}, sent=False)
    if not json_out:
        console.print(f"[green]→[/green] magic packet for {dev.name} "
                      f"[dim]({', '.join(macs)})[/dim] sent "
                      + ("from here" if used.device is None else f"via {used.name}"))
    if no_wait:
        _emit({"ok": True, **result, "sent": True, "woke": False, "waited_s": 0,
               "status": status}, json_out)
        return

    started = time.monotonic()
    deadline = started + max(timeout, 0)
    waiting = (err.status(f"waiting for {dev.name} to answer…")
               if not json_out and err.is_terminal else contextlib.nullcontext())
    with waiting:
        while True:
            rows = _rows([dev.name], refresh=True)
            status = rows[0]["status"] if rows else ""
            if status in _AWAKE or time.monotonic() >= deadline:
                break
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    waited = int(time.monotonic() - started)
    woke = status in _AWAKE
    out = {"ok": woke, **result, "sent": True, "woke": woke, "waited_s": waited,
           "status": status}
    if _emit(out, json_out):
        raise typer.Exit(0 if woke else 1)
    if woke:
        console.print(f"[green]✓[/green] {dev.name} is up [dim]after {waited}s[/dim]"
                      + ("" if status == Status.OK.value
                         else f"  [yellow]{status}[/yellow]: fleet doctor {dev.name} says why"))
        return
    err.print(f"[yellow]{dev.name} did not answer within {waited}s[/yellow] [dim]-- the "
              "packet went out; a machine switched off at the wall, or with Wake-on-LAN "
              f"off in its BIOS, does not wake. fleet doctor {dev.name}[/dim]")
    raise typer.Exit(1)


def _skills_note() -> str:
    """Say so when this machine's agents read an older description of fleet.

    In the human view of `fleet ls` only, the command people actually look at. An update
    refreshes the skills itself now, so this mostly catches a fleet upgraded by hand.
    Never fatal: it is advice, and a read must not fail over it.
    """
    try:
        root = Path.home()
        stale = stale_targets(root, fleet_command())
        # An MCP entry holds no description, only the program to start: one that starts
        # another fleet was called "an older description" too.
        elsewhere = stale_mcp_clients(root, config_command())
    except Exception:
        return ""
    notes = []
    if stale:
        notes.append(f"{', '.join(stale)} read{'s' if len(stale) == 1 else ''} an older "
                     "description of fleet")
    if elsewhere:
        notes.append(f"{', '.join(elsewhere)} start{'s' if len(elsewhere) == 1 else ''} "
                     "another fleet program than this one")
    return "; ".join(notes) + " -- [bold]fleet setup --refresh[/bold]" if notes else ""


def _ago(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@app.command("setup")
def cmd_setup(
    target: list[str] = typer.Option([], "--target",
                                     help="an agent name, or all | auto (whatever is "
                                          "installed; the default); repeat it, or use "
                                          "commas, for several"),
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
    root = Path.cwd() if project else Path.home()
    # MCP clients are a second namespace: a desktop app is registered, not written to.
    # `--project` touches only those that read a config in the repository.

    mcp_names = tuple(c.name for c in MCP_CLIENTS)
    if refresh:
        # Run by every install and update, on the machine being updated. So it may only
        # rewrite what is already there -- never teach an agent fleet that someone chose
        # to leave alone -- and finding nothing to refresh is not a failure.
        from .agents.usage import written_by_newer

        def read_text(path: Path) -> str:
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return ""

        targets = installed_targets(root, project=project)
        clients = [] if project else installed_mcp_clients(root)
        # A copy a newer fleet wrote is not "older": refreshed from an older fleet on the
        # same machine (a checkout beside the uv install, a remote's own copy), it put the
        # older words back over the newer ones.
        paths = plan(root, project=project)
        newer = [t for t in targets if written_by_newer(read_text(paths[t]))]
        targets = [t for t in targets if t not in newer]
        if newer:
            console.print(f"[dim]· kept: {', '.join(newer)} "
                          f"read{'s' if len(newer) == 1 else ''} what a newer fleet "
                          "wrote[/dim]")
        if not targets and not clients and newer:
            return
        if not targets and not clients:
            console.print("[dim]· no agent here has fleet installed; nothing to refresh"
                          "[/dim]")
            return
        changes = (install(root, targets, fleet_command(), dry_run=dry_run, project=project)
                   + install_mcp(root, clients, config_command(), dry_run=dry_run))
        changed = [c for c in changes if c.action != "unchanged"]
        for c in changed:
            _print_change(c, dry_run)
        if not changed:
            console.print("[dim]· agent skills already match this fleet[/dim]")
        return
    # Several at once: `--target claude --target codex` kept the last alone, silently, and
    # an agent's skill shared with another (~/.agents/skills) could only be taken out
    # of one by naming both -- each alone answered "kept for" the other.
    names = list(dict.fromkeys(t.strip() for item in target for t in item.split(",")
                               if t.strip())) or ["auto"]
    if unknown := [t for t in names if t not in (*TARGETS, *mcp_names, "all", "auto")]:
        err.print(f"[red]Unknown target {unknown[0]!r}[/red]  "
                  f"({' | '.join((*TARGETS, *mcp_names))} | all | auto)")
        raise typer.Exit(2)
    if "all" in names:
        targets = list(TARGETS)
        clients = [] if project else list(mcp_names)
    elif names == ["auto"]:
        # cwd tells us nothing about which agents you use, so a project install
        # assumes the one whose layout is identical in both scopes.
        targets = ["claude"] if project else detect_targets(root)
        clients = [] if project else detect_mcp_clients(root)
    else:
        if "auto" in names:
            names.remove("auto")
            names += [t for t in (["claude"] if project else detect_targets(root))
                      + ([] if project else detect_mcp_clients(root)) if t not in names]
        targets = [t for t in names if t in TARGETS]
        clients = [t for t in names if t in mcp_names]
        per_user = {c.name for c in MCP_CLIENTS if not c.project}
        if project and (one := next((t for t in clients if t in per_user), None)):
            # It wrote .config/Claude/claude_desktop_config.json into the repo, where no
            # desktop app looks, and said "created". Cursor (.cursor/mcp.json) and VS Code
            # (.vscode/mcp.json) read one in a project; VS Code was refused as if not.
            err.print(f"[red]{one} keeps one config for you, not one per project[/red]  "
                      f"[dim]-- run [bold]fleet setup --target {one}[/bold] without "
                      "--project[/dim]")
            raise typer.Exit(2)
    # Only once the target is known to be one: `--target nosuch` recorded this machine
    # and said "✓ fleet is ready here" before refusing.
    if not (remove or dry_run):
        _first_run("setup")
    if not targets and not clients:
        from .agents import registry as agent_registry

        # Each agent's own folder: "~/.opencode" was named for one that lives in ~/.config.
        # And the folder an agent's own variable names: with HERMES_HOME set, "~/.hermes"
        # named a place that was not where it looked.
        looked = sorted({str(Path(os.environ[a.env])) if a.env and os.environ.get(a.env)
                         else "~/" + a.marker for a in agent_registry.AGENTS})
        err.print("[yellow]No coding agent found.[/yellow]  Looked for "
                  f"{', '.join(looked)} and the desktop clients.  "
                  "Force one with [bold]--target claude[/bold].")
        raise typer.Exit(1)

    cmd = fleet_command()
    if remove:
        changes = (uninstall(root, targets, dry_run=dry_run, project=project)
                   + uninstall_mcp(root, clients, dry_run=dry_run, project=project))
    else:

        changes = (install(root, targets, cmd, dry_run=dry_run, project=project)
                   + install_mcp(root, clients, config_command(), dry_run=dry_run,
                                 project=project))

    for c in changes:
        _print_change(c, dry_run)
    if kept := [c for c in changes if c.action.startswith("kept for ")]:
        others = [o for c in kept for o in c.action.removeprefix("kept for ").split(", ")]
        named = " ".join(f"--target {t}" for t in dict.fromkeys([*targets, *others]))
        err.print("[dim]· kept: other agents read the same skill file. To take it out of "
                  f"them too: [bold]fleet setup --uninstall {named}"
                  f"{' --project' if project else ''}[/bold][/dim]")
    for action, why in (("unreadable", "not plain JSON or text fleet can rewrite safely "
                         "(not UTF-8, or JSON with comments or trailing commas, which VS "
                         "Code allows), so it was left exactly as it was; add fleet to it "
                         "by hand"),
                        ("taken", "already has a server of its own called fleet, which "
                         "was left in place")):
        if any(c.action == action for c in changes):
            err.print(f"[yellow]![/yellow] {action}: {why}")
    if not remove:
        # "add fleet to it by hand" gave nothing to add: VS Code's mcp.json usually has
        # comments, so this was the answer every VS Code user with one got.
        from .agents.clients import mcp_entry

        keys = {c.name: c.key for c in MCP_CLIENTS}
        for c in changes:
            if c.action == "unreadable" and c.target in keys:
                entry = jsonlib.dumps({"fleet": mcp_entry(config_command())})[1:-1]
                err.print(f"  [dim]{escape(c.target)}: under \"{keys[c.target]}\" in "
                          f"{escape(str(c.path))}, add[/dim]  {escape(entry)}")
    if dry_run:
        console.print("\n[dim]--dry-run: nothing was written.[/dim]")
    elif not remove and cmd != "fleet":
        err.print(f"\n[dim]{_short_name_hint(cmd)}[/dim]")


def _print_change(c, dry_run: bool) -> None:
    colour = {"created": "green", "updated": "green", "removed": "yellow",
              "unreadable": "yellow", "taken": "yellow"}.get(c.action, "dim")
    # A file that is still there held more than fleet -- the user's GEMINI.md, an app's
    # MCP config -- and only fleet's part went: "removed" beside its path read as the
    # whole file deleted. As `fleet uninstall` says it.
    kept = c.action == "removed" and (c.path.name != "SKILL.md" if dry_run
                                      else c.path.exists())
    console.print(f"  [{colour}]{c.action:<9}[/{colour}] "
                  f"{'fleet from ' if kept else ''}{escape(str(c.path))}")


def _missing_key(ssh_command: str) -> str:
    """The `-i` key an ssh command names, when there is no such file."""
    try:
        key = parse_ssh_command(ssh_command).identity
    except ValueError:
        return ""
    return key if key and not os.path.exists(os.path.expanduser(key)) else ""


def _short_name_hint(cmd: str) -> str:
    """How to give agents the bare name `fleet`. It said `uv tool update-shell` whatever
    the path: for fleet pip-installed in a venv that changes nothing, and with no uv
    installed the command is not there."""
    import shutil

    from .agents.registry import UVX

    if cmd == UVX:
        return (f"fleet is not installed here, so agents run it as {cmd}. To install it: "
                "[bold]uv tool install agents-fleet[/bold], then [bold]fleet setup[/bold].")
    where = Path(cmd).parent
    how = ("[bold]uv tool update-shell[/bold]"
           if where == Path.home() / ".local" / "bin" and shutil.which("uv")
           else f"put {escape(str(where))} on PATH in your shell's profile")
    return (f"A new shell here does not find `fleet` by name, so agents call it by its "
            f"full path, {escape(cmd)}. To use the short name: {how}, then "
            "[bold]fleet setup[/bold] in a new terminal.")


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
    firstrun.maybe("mcp", lambda line, level="ok": print(
        f"fleet: {'' if level == 'ok' else level + ': '}{Text.from_markup(line).plain}",
        file=sys.stderr))
    try:
        serve_mcp()
    except McpUnavailable as exc:
        # escaped: the message names an extra, and `[mcp]` is rich markup -- unescaped
        # it printed the install command with the extra silently removed, which is the
        # one part of it the reader needs.

        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)


@app.command("uninstall")
def cmd_uninstall(purge: bool = typer.Option(False, "--purge",
                                             help="also delete fleet's files here: its list "
                                                  "of machines, cache and logs"),
                  yes: bool = typer.Option(False, "--yes", "-y",
                                           help="do not ask; needed without a terminal"),
                  json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
    """Take fleet off this machine: out of your agents, and with --purge its files.

    Nothing on the machines fleet looked at changes. The program itself is removed last,
    by you: the command to run is printed at the end.

    [dim]Example:[/dim]  fleet uninstall --purge
    """
    from .ops import uninstall as _un

    if not yes:
        if not sys.stdin.isatty():
            err.print("[red]This removes fleet from this machine; run it in a terminal, "
                      "or pass --yes.[/red]")
            raise typer.Exit(2)
        steps = ["remove fleet from your agents (skills, MCP entries)"]
        if purge:
            steps.append("delete fleet's files here: its list of machines, cache and logs")
        console.print("This will:\n" + "\n".join(f"  - {x}" for x in steps))
        if not typer.confirm("Uninstall fleet from this machine?"):
            raise typer.Exit(1)
    out = _un.uninstall(purge=purge)
    if _emit({"ok": True, **dataclasses.asdict(out)}, json_out):
        return
    for path in out.agents:
        # A file that is still there held more than fleet -- an agent's MCP config, an
        # AGENTS.md -- and only fleet's part of it went.
        what = f"fleet from {path}" if Path(path).exists() else path
        console.print(f"[green]✓[/green] removed {what}")
    if out.files_removed:
        console.print(f"[green]✓[/green] deleted {len(out.files_removed)} of fleet's files")
    elif out.files_kept:
        console.print("[dim]· fleet's files are kept (--purge deletes them):[/dim]")
        for path in out.files_kept:
            console.print(f"    [dim]{path}[/dim]")
    console.print("\nLast, remove the program itself:")
    for line in out.remove_program:
        console.print(f"  [bold]{line}[/bold]")


@app.command("paths")
def cmd_paths():
    """Show where fleet keeps its state.

    [dim]Example:[/dim]  fleet paths
    """

    # Read through the modules that own these, not through this module's import-time
    # copies. `from .config import INVENTORY_PATH` binds the value once, so a test that
    # redirects state -- and `fleet paths` is the one command whose whole job is to say
    # where state lives -- was printing the real machine's paths from inside the sandbox.
    console.print(f"machines   {inv.INVENTORY_PATH}   [dim](the ones you added; share it "
                  "with your dotfiles if you like)[/dim]")
    console.print(f"config     {_cfg.CONFIG_PATH}")
    console.print(f"cache      {store.DB_PATH}   [dim](disposable — delete and re-probe)[/dim]")
    # Every place the probe reads one: this listed the Linux two alone, and a Mac or a
    # Windows machine keeps its own somewhere else.
    console.print("notes      ~/.config/fleet/machine.yaml, /etc/fleet/machine.yaml   "
                  "[dim](on each machine, about itself: what fleet edit writes there)[/dim]\n"
                  "           [dim]on a Mac also ~/Library/Application Support/fleet/"
                  "machine.yaml; on Windows %LOCALAPPDATA%\\fleet\\machine.yaml and "
                  "%ProgramData%\\fleet\\machine.yaml[/dim]")
    if _cfg.CONFIG_DIR == _cfg.STATE_DIR:
        # Worth saying out loud: it is why every filename above is distinct, and why
        # nothing here may ever be cleaned up by globbing a directory.
        console.print(f"\n[dim]config and state are the same directory here "
                      f"({_cfg.CONFIG_DIR}).[/dim]")


def _not_added(name: str) -> Device | None:
    """A machine `fleet add` refused, as `fleet doctor` is told to look at it.

    `fleet add` records nothing when it cannot get in, and the guides send people to
    `fleet doctor NAME` for the fix -- which answered "No machine named" for exactly the
    machines it is for.
    """
    import dataclasses

    try:
        ep = resolve_command(name if name.lstrip().startswith("ssh ") else f"ssh {name}")
    except Exception:  # noqa: BLE001 - anything ssh will not read is simply not a machine
        return None
    return Device(id="", name=name, endpoints=[dataclasses.asdict(ep)])


@app.command("doctor")
def cmd_doctor(name: str = typer.Argument(None, help="one machine; all that are failing if omitted"),
               json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table, for scripts and agents")):
    """Why a machine will not let fleet in, and the one thing to do about it.

    fleet connects exactly as you do and manages no access, so the fix is yours to make:
    usually putting your key on the machine, adding it to a rental provider's account,
    or a rule in your Tailscale SSH policy. This names the one for each machine. It
    changes nothing.

    [dim]Example:[/dim]  fleet doctor gpu-box
    """
    from .ops import doctor

    devices = inv.live(inv.load())
    if name is not None:
        dev = inv.find(devices, name) or _not_added(name)
        if dev is None:
            err.print(f"[red]No machine named {name!r}[/red]")
            if near := inv.near_matches(devices, name):
                err.print(f"  [dim]did you mean: {escape(', '.join(near))}[/dim]")
            raise typer.Exit(1)
        if not dev.id and not json_out:
            err.print(f"[dim]{escape(name)} is not in your list: looking at it as an ssh "
                      "address, as `fleet add` would[/dim]")
            if near := inv.near_matches(devices, name):
                # `fleet doctor v` for vm or vm-2 looked up a host called v, and said
                # only that no machine goes by that name.
                err.print(f"  [dim]did you mean: {escape(', '.join(near))}[/dim]")
        targets = [dev]
    else:
        conn = store.connect()
        try:
            targets = [d for d in devices if d.endpoints and d.probeable
                       and (store.latest(conn, d.id)[0] or {}).get("status")
                       not in (None, Status.OK.value)]
        finally:
            conn.close()
    conn = store.connect()
    try:
        last = [store.latest(conn, dev.id) for dev in targets]
    finally:
        conn.close()
    # Side by side, as `ls` probes, and each said as soon as it and those before it are
    # done: one after another, twenty machines that were switched off kept `fleet doctor`
    # silent for minutes, and two hundred for longer than anyone waits.
    import concurrent.futures

    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(int(load_config().max_workers), len(targets) or 1)))
    try:
        found = pool.map(lambda job: doctor.diagnose(job[0], state=job[1][0], snap=job[1][1]),
                         zip(targets, last))
        if json_out:
            _emit({"machines": [v.to_dict() for v in found]}, True)
            return
        if not targets:
            remote = [d for d in devices if d.endpoints]
            if not remote:
                # With nothing added, "every machine answered" was said of none.
                console.print("[dim]· fleet watches no machine but this one yet: "
                              "fleet add \"ssh user@host\" adds one[/dim]")
                return
            conn = store.connect()
            try:
                unread = sorted(d.name for d in remote
                                if not (store.latest(conn, d.id)[0] or {}).get("status"))
            finally:
                conn.close()
            if len(unread) == len(remote):
                console.print(f"[dim]· none of the {len(remote)} machines fleet watches "
                              "has been probed yet: fleet doctor NAME looks at one now[/dim]")
                return
            console.print("[green]✓[/green] every machine fleet "
                          + ("has probed" if unread else "watches")
                          + " answered its last probe [dim](fleet doctor NAME looks at one "
                          "now)[/dim]")
            if unread:
                # One never probed (on demand, or a shared host) had not answered anything.
                console.print(f"  [dim]not probed yet: {escape(', '.join(unread[:6]))}"
                              f"{f' and {len(unread) - 6} more' if len(unread) > 6 else ''}"
                              "[/dim]")
            return
        _print_verdicts(found)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _print_verdicts(verdicts) -> None:
    for v in verdicts:
        mark = "[green]✓[/green]" if v.status == Status.OK.value else "[red]✗[/red]"
        console.print(f"{mark} [bold]{v.name}[/bold]  [dim]{v.status}"
                      + (f" · {escape(v.server)}" if v.server else "")
                      + (f" · {escape(v.route)}" if v.route else "") + "[/dim]")
        console.print(f"  {escape(v.why)}")
        for line in v.fix:
            console.print(f"    {escape(line)}", soft_wrap=True)


def _reflowed(doc: str) -> str:
    """A docstring with each paragraph on one line, for Rich to wrap to the terminal.

    Rich wraps a line that is too long and keeps the docstring's own line breaks too, so
    in an 80-column terminal every help page had a word or two stranded on a line of its
    own ("its", "with", "ssh,").
    """
    import inspect

    return "\n\n".join(" ".join(ln.strip() for ln in para.splitlines() if ln.strip())
                       for para in inspect.cleandoc(doc).split("\n\n"))


for _command in app.registered_commands:
    if _command.help is None and _command.callback is not None and _command.callback.__doc__:
        _command.callback.__doc__ = _reflowed(_command.callback.__doc__)


def main() -> None:
    """Run the CLI. Failures a person can act on end in one line, not a traceback.

    A malformed list of machines, a write that could not get its turn or a folder it
    may not write, or a machine that never answered used to surface as a Python traceback -- and under
    `--json`, an agent got an empty stdout and no reason. Those now print what went
    wrong (as JSON too, with `--json`) and exit 2. Anything else is a bug, and keeps its
    traceback, which is what a bug report needs.
    """
    import subprocess

    from .state.writes import CannotWrite, QueueTimeout

    expected = (inv.InventoryError, QueueTimeout, CannotWrite, subprocess.TimeoutExpired)
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
