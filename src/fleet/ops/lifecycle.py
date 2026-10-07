"""Starting a fleet, leaving one, and taking one down.

The asymmetry here is deliberate and worth keeping in view: `--leave` is a machine's own
decision and needs nobody's permission, because you own the machine you are standing on.
`--dissolve` is the centre's, and it removes every key from every machine *before* it
forgets the fleet -- deleting the access list by hand orphans those keys instead, with
nothing left able to remove them.
"""

from __future__ import annotations

import subprocess
import time
from contextlib import suppress

from ..state import access as acl
from ..state import inventory as inv
from .. import reconcile as rec
from ..ssh.authkeys import sync_command
from ..ssh.cmd import local_platform, local_shell_argv, remote_platform
from ..state import store
from ..ui import console, err
from ..ui import confirm
from .errors import FleetError
from .sweep import endpoint_for

def membership() -> str:
    """Is this machine in a fleet, and does it decide? `center`, `member`, or `""`.

    A member holds no access list -- only the center does -- so membership there is the
    signed cache the sweep leaves behind. Checking for either is what lets `fleet add`
    run anywhere while still refusing on a machine that is in no fleet at all.
    """
    try:
        acc = acl.load()
    except acl.AccessError:
        return "member" if acl.CACHE_PATH.exists() else ""
    return "center" if acl.is_center(acc) else "member"

def dissolve(acc, *, force: bool) -> None:
    """Take the fleet down: every key off every machine, then forget it existed.

    The counterpart to `--init`, and it was missing. Deleting access.yaml by hand does
    not dissolve anything -- it orphans it: `access.load` then raises, so the center can
    no longer manage the fleet, and every machine keeps its keys with no tooling able to
    reach them. The worst of both, arrived at silently.

    Order matters and is not negotiable. Keys come off first; the list is forgotten only
    once they are gone, because the list is the only record of where they were put.
    """
    if not acl.is_center(acc):
        err.print("[red]Only the center can dissolve the fleet.[/red]")
        err.print("  [dim]to remove just this machine, use [bold]fleet center "
                  "--leave[/bold][/dim]")
        raise FleetError("only the center can dissolve the fleet", code=2)

    machines = [m.get("name", fp[:18]) for fp, m in acc.keys.items() if fp != acc.center]
    console.print(f"[yellow]This removes fleet {acc.fleet_id}'s keys from "
                  f"{len(machines)} machine(s):[/yellow] {', '.join(sorted(machines))}")
    console.print("[dim]Access granted through this fleet stops working. Keys you "
                  "installed by hand are untouched.[/dim]")
    if not force and not confirm("Dissolve it?"):
        raise FleetError("cancelled", code=1)

    # Every edge becomes desired-absent, including the center's own -- which `revoke`
    # refuses to express, and rightly: on any other day it would strand a machine.
    ledger = rec.plan(acc, rec.load_ledger())
    now = int(time.time())
    for st in ledger.values():
        st.desired, st.pending_since = "absent", now
    # Onto the list as it is now: the confirmation above may have waited minutes.
    acc, _ = acl.update(lambda current: setattr(current, "allow", []))

    devices = {d.id: d for d in inv.live(inv.load())}
    left, gone = [], set()
    # The center's own key last on each machine: it is the key every removal logs in
    # with. Taken off first, every other block on that machine was refused, and a machine
    # left clean was reported as still holding keys (found on a real five-machine fleet).
    edges = sorted(ledger.items(), key=lambda kv: kv[0].split(">")[0] == acc.center)
    conn = store.connect()
    try:
        for key, st in edges:
            src, dst, user = key.split(">")
            if dst not in acc.keys:
                # Removed with `fleet rm`, which took its keys off it then and said so if
                # it could not. Listed here it was a bare fingerprint, as if still held.
                continue
            dev = devices.get(st.dst_device or (acc.keys.get(dst) or {}).get("device_id", ""))
            if dev is None or not inv.endpoints_of(dev):
                left.append(((acc.keys.get(dst) or {}).get("name", dst[:18]),
                             "no route recorded"))
                continue
            ep = endpoint_for(dev, user)
            _, snap = store.latest(conn, dev.id)
            ok, out = rec.apply_edge(acc, (src, dst, user), ep, install=False,
                                     platform=remote_platform(snap))
            st.attempts += 1
            if ok:
                st.observed = "absent"
                if dev.name not in gone:
                    console.print(f"[green]✓[/green] keys removed from {dev.name}")
                gone.add(dev.name)
            else:
                st.last_error = out
                left.append((dev.name, out[:60]))
                console.print(f"[red]✗[/red] {dev.name} [dim]{out[:50]}[/dim]")
    finally:
        conn.close()
        # Whatever happens next -- a refusal below, Ctrl+C, a machine that hung -- the
        # removals already done are recorded, or the next attempt reads keys that are
        # gone as still present.
        rec.save_ledger(ledger)

    # One line per machine, not per edge: a machine holding three grants is one machine
    # to visit, and listing it three times read as three machines.
    seen: set[str] = set()
    left = [(n, why) for n, why in left if not (n in seen or seen.add(n))]
    if left and not force:
        rec.save_ledger(ledger)
        err.print(f"\n[yellow]{len(left)} machine(s) still hold keys[/yellow] and the "
                  "fleet is kept so you can finish:")
        for name, why in left:
            err.print(f"  {name}: {why}")
        err.print("\n  [dim]run this again when they are reachable, or [bold]--force"
                  "[/bold] to forget the fleet anyway -- those keys then stay installed "
                  "with nothing left to remove them[/dim]")
        raise FleetError("some machines could not be reached", code=1)

    for path in (acl.ACCESS_PATH, acl.LEDGER_PATH, acl.CACHE_PATH, acl.OUTBOX_PATH,
                 acl.CHAIN_PATH, acl.INBOX_PATH, acl.HANDING_PATH):
        with suppress(OSError):
            path.unlink()
    # The listener has no fleet to serve now, and left installed it was restarted by the
    # service manager for good, exiting each time.
    from .. import service
    with suppress(Exception):
        service.remove()
    console.print(f"\n[green]✓[/green] fleet {acc.fleet_id} dissolved; "
                  f"keys removed from {len(gone)} machine(s).")
    if left:
        err.print(f"[yellow]![/yellow] {len(left)} machine(s) kept their keys and there "
                  "is no longer any record of them. Remove them by hand:")
        for name, _ in left:
            err.print(f"  {name}")

def leave(acc) -> None:
    """Strip this fleet's keys from this machine. No permission required.

    You own your machines; the center does not get a veto. It cannot reliably tell
    'left' from 'down' either -- both look like an auth failure -- so this is a courtesy
    to the center as much as a right of the machine.
    """
    # Local, not remote: this edits the file on the machine you are standing on. So the
    # platform is ours, not a probed host's -- and on Windows there is no `sh` at all,
    # which made leaving a fleet impossible from the very machines most likely to want to.
    shell = local_shell_argv()
    for fp in acc.keys:
        script = sync_command(acc.fleet_id, fp, pubkey=None, platform=local_platform())
        subprocess.run(shell, input=script.encode(), capture_output=True)
    console.print(f"[green]✓[/green] removed fleet {acc.fleet_id}'s keys from this machine.")
    console.print("  [dim]the center will see this as unreachable until you tell it[/dim]")


def retire_empty_center(acc) -> bool:
    """Forget a fleet that never had anything in it, so this machine can join another.

    Every install makes its machine a center (so fleet is useful at once), which means a
    machine meant to join someone else's fleet is, by then, the center of an empty one.
    Empty is the whole condition: no other machine pinned and no grant, so no key of
    this fleet sits anywhere but here, and forgetting it strands nothing. A center with
    machines in it must be dissolved or handed over deliberately -- never here.
    """
    if not acl.is_center(acc) or set(acc.keys) - {acc.center} or acc.allow:
        return False
    from .. import service
    from ..state import invites

    with suppress(Exception):
        service.remove()                   # its listener would refuse to serve, forever
    from ..state.writes import turn

    for path in (acl.ACCESS_PATH, acl.LEDGER_PATH, acl.CHAIN_PATH, acl.HANDING_PATH,
                 invites.INVITES_PATH):
        with turn(path), suppress(OSError):  # after any write already queued on it
            path.unlink()

    def demote(current):
        for d in current:
            if d.role == "center":
                d.role = "none"
                inv.touch(d)
        return current, None
    inv.update(demote)
    return True

