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
from ..state import clock, store
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

def settle_center_edge(device_id: str) -> None:
    """Make the center's own access to a just-enrolled machine true now, and recorded.

    It used to wait for a sweep, so `fleet access` reported "not applied yet" for a key
    the center had just used to read the machine's own. Applying the edge here writes
    the labelled block if it is not already there -- idempotent -- and records it in the
    ledger as present.
    """
    try:
        acc = acl.load()
    except acl.AccessError:
        return
    for fp, meta in acc.keys.items():
        if fp != acc.center and meta.get("device_id") == device_id:
            apply_now(acc, acc.center, fp, meta.get("user", "root"), install=True)


def remove_now(acc, dev) -> tuple[int, list[str]]:
    """Revoke everything a machine being removed touches, now, while it can be found.

    Two directions, and `fleet rm` used to leave both to a sweep that could do only one.
    Its key on other machines comes off there -- that is the access being revoked, and
    revokes are pushed, never waited for. And every key this fleet put *on* it comes off
    it, the center's included: removing a machine you sold or gave back must not leave
    the center able to log into it. That second half was never done at all, because the
    record is tombstoned straight after and no sweep can reach a machine with no record.

    Returns (keys removed, what could not be reached). The caller drops the pins after.
    """
    fps = {fp for fp, m in acc.keys.items() if m.get("device_id") == dev.id}
    if not fps:
        return 0, []
    ledger = rec.plan(acc, rec.load_ledger())
    by_id = {d.id: d for d in inv.live(inv.load())}
    removed, unreached = 0, []
    conn = store.connect()
    try:
        # The center's own key last: every removal logs in with it. In fingerprint order
        # it came first on about half of real fleets, and the other blocks on the machine
        # being removed were then refused and left behind (found on a five-machine fleet).
        for key, st in sorted(ledger.items(),
                              key=lambda kv: (kv[0].split(">")[0] == acc.center, kv[0])):
            src, dst, user = key.split(">")
            if src not in fps and dst not in fps:
                continue
            target = dev if dst in fps else by_id.get(
                st.dst_device or (acc.keys.get(dst) or {}).get("device_id", ""))
            ep = endpoint_for(target, user) if target is not None else None
            if ep is None:
                if dst not in fps:
                    unreached.append(f"{acc.name_of(dst)}: no endpoint recorded")
                    st.desired = "absent"
                else:
                    ledger.pop(key, None)
                    unreached.append(f"{dev.name}: no endpoint recorded")
                continue
            _, snap = store.latest(conn, target.id)
            ok, out = rec.apply_edge(acc, (src, dst, user), ep, install=False,
                                     platform=remote_platform(snap))
            st.desired, st.attempts = "absent", st.attempts + 1
            if ok:
                st.observed, st.last_error = "absent", ""
                removed += 1
                console.print(f"  [green]✓[/green] {acc.name_of(src)}'s key removed "
                              f"from {target.name}")
            elif dst in fps:
                # Nothing will ever retry this: the machine is about to have no record.
                # Say so now, with what to do, rather than leave a row pending forever.
                ledger.pop(key, None)
                unreached.append(f"{dev.name}: {out.strip()[:80]}")
            else:
                st.last_error = out
                unreached.append(f"{target.name}: {out.strip()[:80]} (retried by sync)")
    finally:
        conn.close()
    rec.save_ledger(ledger)
    return removed, unreached


def apply_now(acc, src: str, dst: str, user: str, *, install: bool) -> None:
    """Reconcile one edge immediately, on the machine it affects.

    Targeted, not a sweep: one connection to the machine whose authorized_keys changes.
    An unreachable target is not an error -- the ledger keeps it pending with an age and
    a retry count, which is the honest report and what `fleet access` already shows.
    """

    # By the device the key was pinned for, not by name: the access list keeps the name a
    # machine had when it was pinned, and a renamed machine's old name may by now belong
    # to another one -- which then received the grant, while the ledger said it landed.
    devices = inv.live(inv.load())
    did = (acc.keys.get(dst) or {}).get("device_id", "")
    dev = next((d for d in devices if did and d.id == did), None)
    if dev is None and not did:
        dev = inv.find_exact(devices, acc.name_of(dst))   # a pin from before device ids
    if dev is None:
        console.print(f"  [yellow]·[/yellow] {acc.name_of(dst)} is not in the inventory "
                      "[dim]-- it stays pending[/dim]")
        return
    ep = endpoint_for(dev, user)
    if ep is None:
        console.print(f"  [yellow]·[/yellow] {dev.name} has no address to reach it on "
                      "[dim]-- it stays pending[/dim]")
        return
    ledger = rec.load_ledger()
    key = ">".join((src, dst, user))
    st = ledger.get(key) or rec.EdgeState()
    conn = store.connect()
    try:
        _, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    ok, out, install = rec.converge_edge(acc, (src, dst, user), ep, install=install,
                                         platform=remote_platform(snap))
    st.desired = "present" if install else "absent"
    st.dst_device = st.dst_device or dev.id
    if ok:
        st.observed = st.desired
        st.last_error = ""
        console.print(f"  [green]✓[/green] applied on {dev.name}")
    else:
        st.attempts += 1
        st.last_attempt_at = int(time.time())
        st.pending_since = st.pending_since or st.last_attempt_at
        st.last_error = out
        console.print(f"  [yellow]·[/yellow] {dev.name} not reached [dim]({out[:60]})[/dim]")
        console.print("  [dim]it stays pending; the center tries again by itself while it "
                      "is listening, and `fleet sync` there tries now[/dim]")
    ledger[key] = st
    rec.save_ledger(ledger)

def enrol_unpinned(acc, devices) -> bool:
    """Register every machine the access list has no key for. Returns whether any were.

    This is what replaces a separate enrol command. A machine added from a member, or one
    whose enrolment was interrupted, is reachable and ungrantable: the list is keyed on
    the fingerprint of *its* key, so an edge from it cannot even be expressed. Only the
    center can fix that, and a sweep is the moment it is already dialling everything.

    Never prompts. A sweep is unattended, so a host that accepts no key from here is
    reported, not asked about -- the way out is to put the center's key on it, which
    `fleet center --pubkey` prints, rather than to find someone to type a password.
    """
    pinned = {v.get("device_id") for v in acc.keys.values()}
    # A name only stands in for an id on pins made before ids were recorded. Skipping any
    # machine whose *name* was pinned meant a new machine that took a renamed one's old
    # name was never enrolled at all.
    named = {v.get("name") for v in acc.keys.values() if not v.get("device_id")}
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

    Only the center reaches here, and only when `fleet sync` is run deliberately: it also
    enrols machines nobody has decided about yet and hands the inventory round, which is
    not something to do from a background timer nobody is watching. The listener's timer
    only retries what a person already decided -- see `retry_pending`.
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

    pending = [(k, st) for k, st in ledger.items() if not st.converged]
    if not pending:
        # Still hand the inventory round. Keys converging is the common case, and it is
        # exactly when a member has nothing else to learn from -- returning here meant a
        # settled fleet never told anyone anything.
        console.print("[dim]· access is up to date[/dim]")
        broadcast(devices)
        return

    done, failed = converge_pending(acc, ledger, pending, devices)
    console.print(f"\n[dim]{done} applied, {failed} still pending[/dim]"
                  + ("  [dim]-- `fleet access` shows what is outstanding[/dim]"
                     if failed else ""))
    broadcast(devices)


def converge_pending(acc, ledger, pending, devices) -> tuple[int, int]:
    """Apply these not-yet-converged edges, machine by machine. Returns (done, failed).

    The ledger is saved, with every attempt recorded, whatever happens.
    """
    by_id = {d.id: d for d in inv.live(devices)}
    done = failed = 0
    conn = store.connect()
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
                ok, detail, install = rec.converge_edge(acc, triple, ep, install=install,
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
                    # What was applied last, which is what the list wanted by then.
                    st.desired = st.observed = "present" if install else "absent"
                    st.last_error = ""
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
    return done, failed


def retry_due(st, *, interval_s: float, cap_s: float, now: float) -> bool:
    """Whether a pending edge has waited long enough since its last attempt.

    The wait doubles per failed attempt, from `interval_s` up to `cap_s`, so a machine
    that is off for a week costs a few connection attempts an hour, not one a minute.
    """
    if not st.last_attempt_at:
        return True
    elapsed = clock.since(st.last_attempt_at, now)
    return elapsed is None or elapsed >= clock.backoff(interval_s, st.attempts, cap_s)


def retry_pending(*, interval_s: float) -> tuple[int, int]:
    """The listener's retry: apply grants and revokes that could not be applied yet.

    Only what a person already decided -- edges the access list wants and the ledger has
    not seen land. Nothing new is decided here: no machine is enrolled and the inventory
    is not handed round, which stay with a deliberate `fleet sync`. Without this a revoke
    made while its machine was off stayed undone until somebody remembered to sync, and
    the key sat there meanwhile. Returns (done, failed); (0, 0) when there was nothing
    due or this machine should not act as center now.
    """
    try:
        acc = acl.load()
    except acl.AccessError:
        return 0, 0                         # no list here: never, or no longer, the center
    if not acl.is_center(acc) or acl.HANDING_PATH.exists():
        return 0, 0                         # mid-handover: changes here would not carry over
    ledger = rec.plan(acc, rec.load_ledger())
    if rec.refuses_to_run(acc, ledger):
        return 0, 0
    cfg = load_config()
    now = time.time()
    pending = [(k, st) for k, st in ledger.items()
               if not st.converged and retry_due(st, interval_s=interval_s,
                                                 cap_s=float(cfg.offline_backoff_max_s),
                                                 now=now)]
    if not pending:
        return 0, 0
    return converge_pending(acc, ledger, pending, inv.load())


def retry_forever(interval_s: float, *, stop=None) -> None:
    """Run `retry_pending` every `interval_s` seconds until `stop` (an Event) is set.

    Never raises: a failed round is logged and the next one tries again.
    """
    import threading

    stop = stop or threading.Event()
    while not stop.wait(interval_s):
        try:
            done, failed = retry_pending(interval_s=interval_s)
            if done or failed:
                console.print(f"[dim]retried pending access: {done} applied, "
                              f"{failed} still pending[/dim]")
        except Exception as exc:              # noqa: BLE001 -- a background loop
            console.print(f"[dim]retrying pending access failed: "
                          f"{type(exc).__name__}: {exc}[/dim]")

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
    on anything security-relevant -- the access list is, and it is signed -- so a member
    declining to answer costs nothing.
    """

    mine = inv.dumps(inv.load())
    me = identity.local_device_id()
    routes, senders = [], []
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
            senders.append(dev.id)

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
    for sender, (answer, error) in zip(senders, answers):
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
        # Only what that member is the authority on: its reply is not signed, and a
        # member's word about other machines must not become the fleet's.
        inv.update(lambda current: inv.merge(
            current, inv.from_member(current, returned, sender), authoritative=False))
        reached += 1
    if reached:
        console.print(f"[dim]· inventory handed to {reached} machine(s)[/dim]")
