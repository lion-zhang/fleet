"""Keeping a machine's copy of the fleet current.

The centre protocol, on both sides of the wire: sealing what this machine knows, handing
it over or asking for it, and taking back the merge. It came out of `cli.py` first
because it is what `serve.py` was reaching back into -- the HTTP centre importing the
argument parser was the one real import cycle in the package, and this module is where
it stops.

Nothing here knows about commands. `ensure_fresh` is the whole reason `fleet sync` is
rarely typed any more: a read refreshes its own copy when it has gone stale, and a centre
that is down costs freshness and nothing else.
"""

from __future__ import annotations

import subprocess
import time
from contextlib import suppress

import yaml

from ..state import access as acl
from ..state import inventory as inv
from ..state import store
from ..config import DEFAULT_PORT, load_config
from ..models import Status
from ..ssh.cmd import build_argv, run as sshrun
from ..ui import console

def center_advertise_url(acc, port: int = 0) -> str:
    """Where this center tells machines to come back to.

    Computed whether or not the listener is running, because the sweep is how a machine
    first learns the address: it arrives inside a payload already signed by a key the
    machine has pinned, which is the only channel that can carry it safely. A machine
    that finds nothing listening there falls back to being swept, at the cost of one
    refused connection.
    """
    return f"http://{this_host(acc)}:{port or DEFAULT_PORT}/sync"

def this_host(acc) -> str:
    """The address machines already reach this machine on, for --listen to advertise.

    Taken from the inventory rather than from a socket: what matters is the name the
    fleet already uses, which on an overlay is the only one that resolves everywhere.
    """
    me = inv.find_exact(inv.load(), acc.name_of(acc.center))
    for ep in sorted(inv.endpoints_of(me) if me else [], key=lambda e: e.preference):
        if ep.target:
            return ep.target
    import socket

    return socket.gethostname()

def sealed_envelope(payload: str) -> str:
    """Sign our inventory once, for whoever is about to be handed it.

    Split out of `run_sync` because none of it varies per machine and all of it is
    hostile to being run from several threads at once: it reads the access list, reads
    telemetry out of sqlite, and shells out to `ssh-keygen -Y sign`. Handing the same
    envelope to every machine is also simply less work -- the old shape signed the same
    bytes once per spoke.
    """
    try:
        url = center_advertise_url(acl.load())
    except acl.AccessError:
        url = ""                           # not a center; nothing to advertise
    return acl.seal(payload, telemetry=telemetry_to_relay(), center_url=url)


def send_sealed(ep, sealed: str) -> tuple[int, str]:
    """Hand an already-sealed envelope to one machine and take back its answer.

    The only half that varies per machine, and the only half safe to run concurrently:
    it spawns ssh and reads its pipes, and touches nothing this process shares.
    """
    # a non-interactive shell may not have ~/.local/bin on PATH, which is exactly where
    # `fleet install` puts fleet.
    remote = 'sh -lc \'PATH="$HOME/.local/bin:$PATH" fleet sync --serve\''
    argv = build_argv(ep, remote=remote)
    p = sshrun(argv, input=sealed.encode(), timeout=180)
    out = p.stdout.decode(errors="replace")
    return p.returncode, (out if p.returncode == 0 else out + p.stderr.decode(errors="replace"))


def run_sync(ep, payload: str) -> tuple[int, str]:
    """Seal our inventory and hand it to one machine. Sealed, because the far side runs
    this filter for anyone holding a key on it."""
    return send_sealed(ep, sealed_envelope(payload))

def post(url: str, payload: str, timeout: float = 8.0) -> str | None:
    """One request to the center. None on any failure, which is never fatal here."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=payload.encode(),
                                 headers={"Content-Type": "text/yaml"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode(errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None

def telemetry_to_relay() -> list[dict]:
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

def record_relayed(rows: list) -> None:
    """Store rows the center measured, marked as second-hand.

    Never overwrites a probe we ran: `store.latest` prefers first-hand, so our own
    reading of a machine we can reach always wins over the center's view of it.
    """
    from ..models import ProbeResult, Snapshot

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

def ensure_fresh(*, force: bool = False) -> None:
    """Refresh this machine's copy of the fleet from the center, if it has gone stale.

    Called by the commands that read fleet-wide state. It is why nobody types `fleet
    sync` any more: the machine asks when it needs to know, rather than waiting for the
    center to come round.

    Failure is deliberately almost invisible. Every command that calls this already works
    from local state, and the design's own rule is that a sync outage must not become a
    fleet outage -- so a center that is down, or a machine that has never been told where
    to look, simply carries on with what it has.
    """

    url = acl.center_url()
    pinned = acl.trusted_center_pubkey()
    if not url or not pinned:
        return                             # never been told where to ask, or who to trust
    if not force and int(time.time()) - acl.center_last_seen() < int(
            load_config().sync_ttl_s):
        return
    try:
        payload = acl.seal(inv.dumps(inv.load()), telemetry=telemetry_to_relay())
    except Exception:
        return                             # no key of our own yet; nothing to say
    body = post(url, payload)
    if body is None:
        return
    try:
        note = acl.unseal(body, pinned)
        incoming = inv.loads(note["inventory"])
    except Exception:
        return                             # unsigned, or not from the center we pinned
    inv.update(lambda current: inv.merge(current, incoming, authoritative=True))
    if note["telemetry"]:
        record_relayed(note["telemetry"])
    acl.note_center_seen()
    acl.note_center_url(note["center_url"] or url)

def join(url: str) -> str:
    """Dial a center at an address given by hand, and remember it. Returns a summary.

    `ensure_fresh` cannot start on its own: it needs the center's address *and* the
    center's key, and both only ever arrive in a sealed envelope the center delivers by
    dialling out. A machine that can reach the listener but has never been swept is
    therefore stuck -- pinned in the access list, holding the center's key, a member in
    every sense the center cares about, and with no way to find it. This is the way in
    that does not need the center to reach us, which is the whole point of it listening.

    The trust is the trust the fleet already makes. If we have pinned a center, the reply
    must be signed by it. If we have not, first contact pins whoever answered -- and the
    address came from the person running the command, not from the network. The center
    still decides whether to answer: it refuses a signer it has not pinned, so this
    cannot talk a fleet into admitting a machine it has not already admitted.
    """
    from .errors import FleetError

    try:
        payload = acl.seal(inv.dumps(inv.load()), telemetry=telemetry_to_relay())
    except Exception as exc:
        raise FleetError(f"this machine has no fleet key to introduce itself with: {exc}")

    body = post(url, payload, timeout=20.0)
    if body is None:
        raise FleetError(f"no answer from {url}")

    pinned = acl.trusted_center_pubkey()
    try:
        if not pinned:
            acl.unseal_first_contact(body)         # pins whoever answered
            pinned = acl.trusted_center_pubkey()
        note = acl.unseal(body, pinned)
    except acl.AccessError as exc:
        raise FleetError(f"the answer from {url} is not one we can trust: {exc}")

    try:
        incoming = inv.loads(note["inventory"])
    except Exception as exc:
        raise FleetError(f"unreadable inventory from {url}: {exc}")

    _, changes = inv.update(lambda current: inv.merge(current, incoming,
                                                     authoritative=True))
    if note["telemetry"]:
        record_relayed(note["telemetry"])
    acl.note_center_seen()
    # Whatever the center says to use from now on, falling back to what was typed.
    acl.note_center_url(note["center_url"] or url)
    # Live devices, not records: a fleet that has ever removed a machine carries the
    # tombstone for TOMBSTONE_TTL_S so the deletion can propagate, and counting those
    # told a seven-machine fleet it had joined fourteen.
    return f"joined: {len(inv.live(incoming))} machine(s) known, {changes} changed"


def file_request(current, target: str, allow: str, user: str) -> None:
    """Ask the center for an edge we cannot create ourselves.

    Written to our own outbox and carried by the next sweep. A request is not a grant --
    the center decides -- but a request matching an edge that already exists is simply
    key placement that failed, and reconciles without anyone being asked.
    """

    out = []
    if acl.OUTBOX_PATH.exists():
        out = (yaml.safe_load(acl.OUTBOX_PATH.read_text()) or {}).get("requests", [])
    entry = {"to": target, "from": allow, "user": user, "at": int(time.time())}
    if entry not in [{k: v for k, v in r.items() if k != "at"} | {"at": r.get("at")}
                     for r in out]:
        out.append(entry)
    acl.OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    acl.OUTBOX_PATH.write_text(yaml.safe_dump({"requests": out}, sort_keys=False))
