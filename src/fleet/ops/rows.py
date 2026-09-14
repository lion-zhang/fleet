"""Probing what is stale, and rendering every device from the cache.

`_rows` and `_live_tick` were the same function twice -- group the devices that need a
probe by everything that varies per device, run the local one without a network, fan the
rest out, record what comes back, then build a view of *all* of them from the store. The
copies had drifted, which is what two copies do: only one of them tolerated a probe
raising, and a dropped network took `fleet top` down while `fleet ls` shrugged.

They differ in exactly two ways, and both are now arguments: what makes a device due (a
TTL for a one-shot listing, a backoff Schedule for the live view), and whether a failed
batch is fatal. Everything between those is shared.

`top.py` says the Live loop in `cli.py` is a thin shell around it. That is true now.
"""

from __future__ import annotations

import time

from ..state import inventory as inv
from ..state import store
from ..config import load_config
from ..probe.runner import probe_many, run_probe_local
from ..render.view import Detail, device_view
from . import identity


def _probe(conn, due: list, cfg, *, me: str, schedule=None, tolerant: bool = False) -> None:
    """Probe every device in `due` and record the results.

    Grouped by `(mode, disk_paths)` because `probe_many` applies one set of options to a
    whole batch: a shared host gets the polite probe, and one device's disk paths must
    not leak into another's probe.
    """
    now = time.monotonic()
    groups: dict[tuple[str, tuple[str, ...]], dict[str, list]] = {}
    for d in due:
        if me and d.id == me:
            # no ssh, no key, no network to look at the machine we are running on
            res = run_probe_local(mode=d.probe_mode, disk_paths=d.disk_paths)
            store.record(conn, d.id, res)
            if schedule is not None:
                schedule.record(d.id, ok=res.ok, now=now)
            continue
        groups.setdefault((d.probe_mode, tuple(d.disk_paths)), {})[d.id] = inv.endpoints_of(d)

    for (mode, paths), subset in groups.items():
        try:
            results = probe_many(subset, mode=mode, disk_paths=list(paths),
                                 timeout=float(cfg.probe_timeout_s),
                                 connect_timeout=int(cfg.connect_timeout_s),
                                 max_workers=int(cfg.max_workers))
        except Exception:
            # One unreachable host must not end a live session. probe_many already
            # isolates failures inside a batch; the loop around it has to do the same.
            if not tolerant:
                raise
            if schedule is not None:
                for dev_id in subset:
                    schedule.record(dev_id, ok=False, now=now)
            continue
        for dev_id, res in results.items():
            store.record(conn, dev_id, res)
            if schedule is not None:
                schedule.record(dev_id, ok=res.ok, now=now)


def _render(conn, devices: list, detail: Detail, me: str) -> list[dict]:
    out = []
    for d in devices:
        st, sn = store.latest(conn, d.id)
        out.append(device_view(d, st, sn, detail, self_id=me))
    return out


def snapshot(names: list[str] | None = None, *, refresh: bool = False,
             detail: Detail = Detail.COMPACT) -> list[dict]:
    """Every device (or the named ones), probed if its telemetry has aged out."""
    cfg = load_config()
    devices = inv.live(inv.load())
    if names:
        # By handle, not by name: `ls`, `show` and `top` all filter through here, so an
        # alias that worked for `ssh` and `edit` but not for looking at the machine would
        # be a handle you cannot use for the thing you do most.
        wanted = {d.id for d in (inv.find(devices, n) for n in names) if d}
        devices = [d for d in devices if d.id in wanted]

    conn = store.connect()
    try:
        me = identity.local_device_id()
        stale = []
        for d in devices:
            st, _ = store.latest(conn, d.id)
            eligible = d.probeable or (bool(names) and d.probe_policy != "never")
            if eligible and (refresh or not store.is_fresh(st, int(cfg.telemetry_ttl_s))):
                stale.append(d)
        if stale:
            _probe(conn, stale, cfg, me=me)
        return _render(conn, devices, detail, me)
    finally:
        conn.close()


def tick(conn, devices: list, schedule, cfg, detail: Detail) -> list[dict]:
    """One frame of the live view: probe whatever the schedule says is due, then render.

    The connection and the device list are the caller's, because a live loop holds both
    across every frame -- reopening the store eight times a second to answer the same
    question is the cost this signature exists to avoid.
    """
    me = identity.local_device_id()
    due = [d for d in schedule.due(devices, time.monotonic()) if d.probeable]
    if due:
        _probe(conn, due, cfg, me=me, schedule=schedule, tolerant=True)
    return _render(conn, devices, detail, me)
