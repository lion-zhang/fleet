"""The centre's pass over the fleet.

What the access list says should be true, made so: enrol anything not yet pinned, install
and remove keys, collect telemetry on the way, and hand the inventory to every machine
that can take it.

Only one job here genuinely needs the centre to dial out -- removing a key, because a
passive host will not delete a peer's key on its own. The rest is signed data that
travels either way, which is why a machine can now refresh itself and this runs rarely.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import replace

from ..state import access as acl
from ..state import inventory as inv
from .. import reconcile as rec
from ..state import store
from ..config import load_config
from ..probe.runner import run_probe
from ..ssh.cmd import remote_platform
from ..ui import console, err
from . import enrol
from . import identity
from .errors import FleetError
from . import sync

def _across_machines(jobs: list, work, *, workers: int) -> list[tuple]:
    """Run `work` over `jobs` concurrently. Returns one `(value, error)` per job, in order.

    The pairs are the point, not ceremony. A sweep reaches machines that are switched
    off, rented by the hour, or a NAS that takes longer than the timeout to answer, and
    one of them raising used to end the whole pass -- which under a fan-out is worse than
    it sounds, because every result is collected before any is used, so one slow machine
    would discard what every other machine had just done. A worker that raises returns
    its exception here and the caller decides what that machine's failure means.

    **Across machines only.** `reconcile._remote` passes multiplex=False deliberately --
    sharing one SSH master to the *same* host races on the connection -- and that is
    untouched by dialling different hosts at once. Each job here is one machine's worth
    of work, run start to finish on one thread, so a device's own edges stay serial.

    Results come back in input order rather than as they complete, so the sweep still
    reads top to bottom. The network work is what was slow; the printing never was.

    Nothing here touches sqlite. The connection is not shared across threads, so the
    workers do the dialling and the caller records what came back.
    """
    def guarded(job):
        try:
            return (work(job), None)
        except Exception as exc:           # noqa: BLE001 - deliberately everything
            return (None, exc)

    if len(jobs) < 2 or workers < 2:
        return [guarded(j) for j in jobs]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        return list(pool.map(guarded, jobs))


def endpoint_for(dev, user: str):
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

def apply_now(acc, src: str, dst: str, user: str, *, install: bool) -> None:
    """Reconcile one edge immediately, on the machine it affects.

    Targeted, not a sweep: one connection to the machine whose authorized_keys changes.
    An unreachable target is not an error -- the ledger keeps it pending with an age and
    a retry count, which is the honest report and what `fleet access` already shows.
    """

    dev = inv.find_exact(inv.load(), acc.name_of(dst))
    if dev is None:
        return
    ep = endpoint_for(dev, user)
    if ep is None:
        return
    ledger = rec.load_ledger()
    key = ">".join((src, dst, user))
    st = ledger.get(key) or rec.EdgeState()
    conn = store.connect()
    try:
        _, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    ok, out = rec.apply_edge(acc, (src, dst, user), ep, install=install,
                             platform=remote_platform(snap))
    if ok:
        st.observed = st.desired = "present" if install else "absent"
        st.last_error = ""
        console.print(f"  [green]✓[/green] applied on {dev.name}")
    else:
        st.last_error = out
        console.print(f"  [yellow]·[/yellow] {dev.name} not reached [dim]({out[:60]})[/dim]")
        console.print("  [dim]it stays pending; `fleet sync` retries[/dim]")
    ledger[key] = st
    rec.save_ledger(ledger)

def enrol_unpinned(acc, devices) -> bool:
    """Register every machine the access list has no key for. Returns whether any were.

    This is what replaces a separate enrol command. A machine added from a spoke, or one
    whose enrolment was interrupted, is reachable and ungrantable: the list is keyed on
    the fingerprint of *its* key, so an edge from it cannot even be expressed. Only the
    center can fix that, and a sweep is the moment it is already dialling everything.

    Never prompts. A sweep is unattended, so a host that accepts no key from here is
    reported, not asked about -- the way out is to put the center's key on it, which
    `fleet center --pubkey` prints, rather than to find someone to type a password.
    """
    pinned = {v.get("device_id") for v in acc.keys.values()}
    named = {v.get("name") for v in acc.keys.values()}
    done = False
    for dev in inv.live(devices):
        if dev.id in pinned or dev.name in named:
            continue
        if dev.ssh_auth == "external":
            continue                       # authorized upstream; nothing to pin here
        if not inv.endpoints_of(dev):
            continue                       # the center itself has no endpoint to dial
        done = bool(enrol.register_identity(dev)) or done
    return done

def run(devices) -> None:
    """The center's pass over the fleet: make authorized_keys match the access list.

    Only the center reaches here, and only when `fleet sync` is run deliberately -- this
    installs and removes credentials on every machine, which is not something to do from
    a background timer nobody is watching.
    """

    try:
        acc = acl.load()
    except acl.AccessError as exc:
        console.print(f"[dim]· {exc}[/dim]")
        return

    ledger = rec.plan(acc, rec.load_ledger())
    if why := rec.refuses_to_run(acc, ledger):
        err.print(f"[red]{why}[/red]")
        raise FleetError(why, code=2)

    # After the wipe guard, never before it: a sweep that is about to be refused must not
    # first go and put keys on things. Re-plan afterwards, because an enrolment is what
    # makes an edge from that machine expressible at all.
    if enrol_unpinned(acc, devices):
        acc = acl.load()                   # each enrolment saved a new generation
        ledger = rec.plan(acc, rec.load_ledger())

    by_id = {d.id: d for d in inv.live(devices)}
    pending = [(k, st) for k, st in ledger.items() if not st.converged]
    if not pending:
        # Still hand the inventory round. Keys converging is the common case, and it is
        # exactly when a spoke has nothing else to learn from -- returning here meant a
        # settled fleet never told anyone anything.
        console.print("[dim]· access is up to date[/dim]")
        broadcast(devices)
        return

    conn = store.connect()
    done = failed = 0
    try:
        # Resolve every edge first, in this thread: what device it is about, which route
        # to dial, and what the last probe says the far side runs. All of that reads the
        # inventory and the store, and neither is safe to touch from a worker.
        work: dict[str, list] = {}
        for key, st in pending:
            src, dst, user = key.split(">")
            # the ledger's copy first: a revoke usually runs *because* the machine was
            # dropped from the list, so the pin is often already gone
            dev = by_id.get(st.dst_device or (acc.keys.get(dst) or {}).get("device_id", ""))
            st.attempts += 1
            st.last_attempt_at = int(time.time())
            if dev is None:
                st.last_error = "no device record for this machine"
                failed += 1
                continue
            ep = endpoint_for(dev, user)
            if ep is None:
                st.last_error = "no endpoint recorded"
                failed += 1
                continue
            _, snap = store.latest(conn, dev.id)
            work.setdefault(dev.id, []).append(
                (st, dev, ep, (src, dst, user), st.desired == "present",
                 remote_platform(snap)))

        def one_machine(batch):
            """Every edge on one machine, in order, then a probe if anything landed.

            Grouped by device rather than by edge so that two edges on the same host stay
            serial -- they edit the same authorized_keys, and interleaving them is how a
            marker block gets written twice or lost.
            """
            out = []
            touched = None
            for st, dev, ep, triple, install, platform in batch:
                ok, detail = rec.apply_edge(acc, triple, ep, install=install,
                                            platform=platform)
                out.append((st, dev, triple, install, ok, detail))
                if ok:
                    touched = (dev, ep)
            probe = None
            if touched is not None:
                # While we are connected anyway: a machine that cannot reach this one
                # will otherwise have no telemetry for it at all.
                dev, ep = touched
                with suppress(Exception):
                    probe = (dev.id, run_probe(ep, mode=dev.probe_mode,
                                               disk_paths=dev.disk_paths))
            return out, probe

        cfg = load_config()
        batches = list(work.values())
        for batch, (outcome, error) in zip(
                batches, _across_machines(batches, one_machine,
                                          workers=int(cfg.max_workers))):
            if error is not None:
                # The machine, not the sweep. Every edge in this batch is one machine's,
                # so they all failed for the same reason and the ledger records it.
                for st, dev, _ep, _triple, _install, _platform in batch:
                    st.last_error = f"{type(error).__name__}: {error}"[:200]
                    failed += 1
                    console.print(f"[yellow]·[/yellow] {dev.name} not reached "
                                  f"[dim]({type(error).__name__})[/dim]")
                continue
            results, probe = outcome
            if probe is not None:
                store.record(conn, probe[0], probe[1])
            for st, dev, (src, _dst, _user), install, ok, detail in results:
                if ok:
                    st.observed, st.last_error = st.desired, ""
                    done += 1
                    verb = "installed on" if install else "removed from"
                    console.print(f"[green]✓[/green] {acc.name_of(src)}'s key {verb} {dev.name}")
                else:
                    st.last_error = detail
                    failed += 1
                    # Not an error: a device that is off is an edge that has not converged.
                    console.print(f"[yellow]·[/yellow] {dev.name} not reached "
                                  f"[dim]({detail[:60]})[/dim]")
    finally:
        conn.close()
        rec.save_ledger(ledger)

    console.print(f"\n[dim]{done} applied, {failed} still pending[/dim]"
                  + ("  [dim]-- `fleet access` shows what is outstanding[/dim]"
                     if failed else ""))
    broadcast(devices)

def broadcast(devices) -> None:
    """Hand every machine that runs fleet the current inventory, and with it the center.

    A fallback now rather than the routine path: a machine that can reach the listener
    refreshes itself, and one that cannot -- or that has not been told where to look yet
    -- is told here. That is what stops a machine holding our key in its authorized_keys
    with no idea where the key came from.

    The inventory is read once rather than per machine: this loop used to re-read it from
    disk on every iteration, which on a fleet of any size is the same file parsed N times
    to send N copies of the same thing.

    A machine without fleet installed simply fails this; that is the ordinary case for a
    managed target and is not worth a line of output. The inventory is not the authority
    on anything security-relevant -- the access list is, and it is signed -- so a spoke
    declining to answer costs nothing.
    """

    mine = inv.dumps(inv.load())
    me = identity.local_device_id()
    routes = []
    for dev in inv.live(devices):
        # Never the machine this is running on. "No endpoints" used to stand in for "the
        # center", which held only while the center was a laptop nobody could reach: once
        # it had an address of its own, the center opened an SSH connection to itself to
        # hand itself an inventory it had just written. On Windows that one hung with no
        # timeout and took the sweep with it.
        if me and dev.id == me:
            continue
        eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
        if eps:
            routes.append(eps[0])

    cfg = load_config()
    # Sealed once, here, before any thread starts. Sealing reads the access list, reads
    # telemetry out of sqlite and shells out to `ssh-keygen -Y sign`, and doing that from
    # eight workers at once wedged a real sweep: the threads sat in `sign` while their
    # subprocess reader threads waited on pipes that never closed. It is also the same
    # envelope for every machine, so signing it per machine was only ever extra work.
    sealed = sync.sealed_envelope(mine)
    answers = _across_machines(routes, lambda ep: sync.send_sealed(ep, sealed),
                               workers=int(cfg.max_workers))

    reached = 0
    for answer, error in answers:
        if error is not None:
            # A machine that takes longer than the timeout to answer -- a NAS, a rental
            # that has gone away -- is a machine that did not get the inventory, not a
            # reason to stop handing it to the others.
            continue
        code, output = answer
        if code != 0:
            continue
        try:
            returned = inv.loads(output)
        except Exception:
            continue                       # not fleet on the far side, or an old one
        # Merged against what the file holds *now*, not the list we started the sweep
        # with: the round trips take a while and a `fleet add` may have landed since.
        # Still one at a time, and still here rather than in a worker -- merging is the
        # part that writes.
        inv.update(lambda current: inv.merge(current, returned, authoritative=False))
        reached += 1
    if reached:
        console.print(f"[dim]· inventory handed to {reached} machine(s)[/dim]")
