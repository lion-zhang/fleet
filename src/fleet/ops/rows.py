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

import threading
import time

from ..state import inventory as inv
from ..state import store
from ..config import load_config
from ..models import Kind
from ..probe.runner import probe_many, run_probe_local
from ..render.view import Detail, device_view
from . import identity


def _probe(conn, due: list, cfg, *, me: str, schedule=None, tolerant: bool = False,
           light: bool = False, refresh: bool = False) -> None:
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
            res = run_probe_local(mode=d.probe_mode, disk_paths=d.disk_paths, light=light,
                                  refresh=refresh)
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
                                 max_workers=int(cfg.max_workers),
                                 light=light, refresh=refresh)
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
             detail: Detail = Detail.COMPACT, rescan: bool = False) -> list[dict]:
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
        # Backing off is for the reads nobody aimed at that machine. Naming it -- `fleet
        # ls machine_A`, `fleet show machine_A` -- is asking about it, so it is asked.
        backoff = 0 if names else int(cfg.offline_backoff_max_s or 0)
        for d in devices:
            st, _ = store.latest(conn, d.id)
            # A mobile machine is never measured (docs/reference/facts.md), named or not.
            eligible = d.probeable or (bool(names) and d.probe_policy != "never"
                                       and d.kind is not Kind.MOBILE)
            ttl = int(cfg.telemetry_ttl_s)
            if d.kind == Kind.SHARED:
                # A multi-user cluster is not measured more often than this, by `ls` and
                # `show` as by `top` -- before, every agent's `ls` measured it every
                # minute. `-r` naming it still asks now.
                ttl = max(ttl, int(cfg.shared_min_interval_s or 0))
            if eligible and (refresh or rescan
                             or not store.is_fresh(st, ttl, backoff_max=backoff)):
                stale.append(d)
        if stale:
            # `rescan` also rebuilds what the machines cache for hours -- software versions
            # and folder sizes -- where `refresh` only asks for a reading now.
            _probe(conn, stale, cfg, me=me, refresh=rescan)
        return _render(conn, devices, detail, me)
    finally:
        conn.close()


def render(conn, devices: list, detail: Detail) -> list[dict]:
    """Every device as the store has it, probing nothing."""
    return _render(conn, devices, detail, identity.local_device_id())


class Background:
    """The live view's probing, off the thread that draws and reads keys.

    Done inside each frame, the frame waited for every machine due: one switched off
    held it for the whole probe timeout, and a list with dozens of them left `fleet top`
    a blank screen for a minute that `q` could not end. Here each machine is asked on
    its own, up to `max_workers` at once, so one that answers is redrawn as soon as it
    has, and the window shows what the store already knows from the first frame.
    Threads are daemons: quitting does not wait for a host that never answers.
    """

    def __init__(self, devices: list, new_schedule, cfg, *, named: bool = False):
        self._devices, self._new, self._cfg, self._named = devices, new_schedule, cfg, named
        self.schedule = new_schedule()
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, int(cfg.max_workers)))
        self._stop, self._wake = threading.Event(), threading.Event()
        self._me = identity.local_device_id()
        self._thread = threading.Thread(target=self._run, name="fleet-top", daemon=True)

    def start(self) -> Background:
        self._thread.start()
        return self

    def again(self) -> None:
        """Everything due at once (the `r` key)."""
        with self._lock:
            self.schedule = self._new()
        self._wake.set()

    def follow(self, devices: list) -> None:
        """The list as it is now, after another `fleet` changed it."""
        with self._lock:
            self._devices = devices
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                schedule = self.schedule
                due = [d for d in schedule.due(self._devices, time.monotonic())
                       if d.id not in self._busy and (
                           d.probeable or (self._named and d.probe_policy != "never"
                                           and d.kind is not Kind.MOBILE))]
                self._busy.update(d.id for d in due)
            for d in due:
                self._slots.acquire()
                if self._stop.is_set():
                    return
                threading.Thread(target=self._one, args=(d, schedule), daemon=True).start()
            self._wake.wait(0.5)
            self._wake.clear()

    def _one(self, dev, schedule) -> None:
        try:
            conn = store.connect()
            try:
                # Light, as in `tick`: a frame every few seconds cannot afford to ask
                # every LLM server for its models.
                _probe(conn, [dev], self._cfg, me=self._me, schedule=schedule,
                       tolerant=True, light=True)
            finally:
                conn.close()
        except Exception:       # noqa: BLE001 - one machine must not end the live view
            schedule.record(dev.id, ok=False, now=time.monotonic())
        finally:
            with self._lock:
                self._busy.discard(dev.id)
            self._slots.release()


def tick(conn, devices: list, schedule, cfg, detail: Detail, *,
         named: bool = False) -> list[dict]:
    """One frame of the live view: probe whatever the schedule says is due, then render.

    The connection and the device list are the caller's, because a live loop holds both
    across every frame -- reopening the store eight times a second to answer the same
    question is the cost this signature exists to avoid.
    """
    me = identity.local_device_id()
    # As in `snapshot`: naming a machine is asking about it. `fleet top login01` on a
    # shared host never measured it at all, and its "2m old" only grew.
    due = [d for d in schedule.due(devices, time.monotonic())
           if d.probeable or (named and d.probe_policy != "never"
                              and d.kind is not Kind.MOBILE)]
    if due:
        # Light: what the machines have cached, and nothing that waits on the network --
        # a frame every few seconds cannot afford to ask every LLM server for its models.
        _probe(conn, due, cfg, me=me, schedule=schedule, tolerant=True, light=True)
    return _render(conn, devices, detail, me)
