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
from ..state import clock, store
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
        # Never a loopback name: the center reached as `ssh me@localhost` is a real
        # endpoint for it, and advertised it sent every invited machine to dial itself.
        if ep.target and not _loopback(ep.target):
            return ep.target
    import socket

    return socket.gethostname()

def _loopback(target: str) -> bool:
    import ipaddress

    if target.lower() in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(target.strip("[]")).is_loopback
    except ValueError:
        return False


def sealed_envelope(payload: str) -> str:
    """Sign our inventory once, for whoever is about to be handed it.

    Split out of `run_sync` because none of it varies per machine and all of it is
    hostile to being run from several threads at once: it reads the access list, reads
    telemetry out of sqlite, and shells out to `ssh-keygen -Y sign`. Handing the same
    envelope to every machine is also simply less work -- the old shape signed the same
    bytes once per member.
    """
    try:
        acc = acl.load()
        url, fleet_id = center_advertise_url(acc), acc.fleet_id
    except acl.AccessError:
        url, fleet_id = "", ""             # not a center; nothing to advertise
    return acl.seal(payload, telemetry=telemetry_to_relay(), center_url=url,
                    fleet_id=fleet_id)


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

def post(url: str, payload: str, timeout: float = 8.0, *, errors: bool = False):
    """One request to the center. None on any failure, which is never fatal here.

    With `errors`, a failure is `(status, text)` instead -- status None when nothing
    answered -- for the one caller that has a person waiting on why: a refused join
    says which way the invite was wrong, and "no answer" would hide that.
    """
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "text/yaml"}
    # Say who signed it up front, so the center can refuse a stranger without reading
    # the body (serve.SIGNER_HEADER).
    try:
        headers["X-Fleet-Signer"] = acl.fingerprint(acl.claimed_signer(payload))
    except Exception:
        pass
    req = urllib.request.Request(url, data=payload.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        if not errors:
            return None
        try:
            text = exc.read().decode(errors="replace")
        except OSError:
            text = ""
        return exc.code, text or str(exc.reason)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return (None, str(getattr(exc, "reason", "") or exc)) if errors else None

def telemetry_to_relay() -> list[dict]:
    """What we measured ourselves, for machines the far side may not be able to reach.

    First-hand only: relaying a row that was itself relayed would let a reading drift
    between machines with nothing to say how far it had travelled or how old it really
    was.
    """
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT device_id, status, last_probe_at, error_class, error_detail "
            "FROM device_state WHERE source='self'").fetchall()
        out = []
        for r in rows:
            _, snap = store.latest(conn, r["device_id"])
            out.append({"device_id": r["device_id"], "status": r["status"],
                        # in the center's clock: a member converts its own
                        "probed_at": (r["last_probe_at"] or 0) + clock.offset()
                        if r["last_probe_at"] else r["last_probe_at"],
                        "snapshot": snap,
                        "error_class": r["error_class"] or "",
                        "error_detail": r["error_detail"] or ""})
        return out
    finally:
        conn.close()

def record_relayed(rows: list, *, sender_id: str | None = None,
                   by: str = "center") -> None:
    """Store readings another machine took, marked as second-hand.

    Never overwrites a probe we ran: `store.latest` prefers first-hand, so our own
    reading of a machine we can reach always wins over someone else's view of it.

    `sender_id` is set when the center takes readings from a member. Then a member is
    believed about itself, and about machines the center has never measured itself --
    the ones it cannot reach, which is why it would want a member's reading at all. About
    a machine the center measures, a member's word could only make it look freer or
    busier than it is, which steers where work goes; that is left out.
    """
    from ..models import ProbeResult, Snapshot

    conn = store.connect()
    try:
        measured: set[str] = set()
        if sender_id is not None:
            measured = {r["device_id"] for r in conn.execute(
                "SELECT device_id FROM device_state WHERE source='self'").fetchall()}
        for row in rows:
            if not isinstance(row, dict) or not row.get("device_id"):
                continue
            if (sender_id is not None and row["device_id"] != sender_id
                    and row["device_id"] in measured):
                continue
            snap = None
            if isinstance(row.get("snapshot"), dict):
                with suppress(Exception):
                    snap = Snapshot(**{k: v for k, v in row["snapshot"].items()
                                       if k in Snapshot.__dataclass_fields__})
            at = row.get("probed_at")
            with suppress(ValueError, TypeError):
                store.record(conn, row["device_id"],
                             ProbeResult(status=Status(row.get("status") or "unknown"),
                                         snapshot=snap,
                                         error_class=str(row.get("error_class") or ""),
                                         error_detail=str(row.get("error_detail") or "")[:200]),
                             source="broadcast", probed_by=by or "center",
                             # the sender's clock is the center's; ours may not be
                             at=clock.to_local(int(at)) if at else None,
                             only_if_newer=True)
    finally:
        conn.close()

def _heard_within(seconds: int) -> bool:
    """Whether the center answered in the last `seconds`. Never, if the clock went back."""
    elapsed = clock.since(acl.center_last_seen())
    return elapsed is not None and elapsed < seconds


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
    cfg = load_config()
    if not force and _heard_within(int(cfg.sync_ttl_s)):
        return
    # A center that did not answer last time is left alone for a while, doubling per
    # miss. Reads keep working from local state either way; what this saves is the
    # connect timeout every one of them was paying to rediscover that it is away.
    if not force and acl.center_retry_after(60, int(cfg.offline_backoff_max_s or 0)):
        return
    from ..config import STATE_DIR
    from ..state.writes import try_turn

    # One refresh at a time is enough. Several agents reading the fleet at once each
    # went to the center for the same answer; now one asks and the others read what is
    # on disk, without waiting for it.
    with try_turn(STATE_DIR / "refresh") as ours:
        if not ours:
            return
        if not force and _heard_within(int(cfg.sync_ttl_s)):
            return                         # refreshed by another while we got here
        _refresh(url, pinned)


def _refresh(url: str, pinned: str) -> None:
    """Ask the center for a fresh copy, and keep what it says. One process at a time."""
    try:
        payload = acl.seal(inv.dumps(inv.load()), telemetry=telemetry_to_relay())
    except Exception:
        return                             # no key of our own yet; nothing to say
    body = post(url, payload)
    if body is None:
        acl.note_center_unanswered()
        return
    try:
        note, signer = acl.unseal_trusting(body, pinned)
        incoming = inv.loads(note["inventory"])
    except Exception:
        # unsigned, or not from the center we pinned. Backed off too: asking again
        # will not change who answers, and it costs a round trip each time.
        acl.note_center_unanswered()
        return
    if signer != pinned:
        acl.pin_center_pubkey(signer)      # a signed handover led here from our pin
    clock.note_center_time(note.get("sent_at", 0))
    inv.update(lambda current: inv.merge_from_center(current, incoming,
                                                     sent_at=note.get("sent_at", 0)))
    if note["telemetry"]:
        record_relayed(note["telemetry"])
    acl.note_center_seen()
    acl.note_center_url(note["center_url"] or url)
    acl.note_fleet_id(note.get("fleet_id", ""))

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
        note, signer = acl.unseal_trusting(body, pinned)
        if signer != pinned:
            acl.pin_center_pubkey(signer)
    except acl.AccessError as exc:
        raise FleetError(f"the answer from {url} is not one we can trust: {exc}")

    try:
        incoming = inv.loads(note["inventory"])
    except Exception as exc:
        raise FleetError(f"unreadable inventory from {url}: {exc}")

    clock.note_center_time(note.get("sent_at", 0))
    _, changes = inv.update(lambda current: inv.merge_from_center(
        current, incoming, sent_at=note.get("sent_at", 0)))
    if note["telemetry"]:
        record_relayed(note["telemetry"])
    acl.note_center_seen()
    # Whatever the center says to use from now on, falling back to what was typed.
    acl.note_center_url(note["center_url"] or url)
    acl.note_fleet_id(note.get("fleet_id", ""))
    # Live devices, not records: a fleet that has ever removed a machine carries the
    # tombstone for TOMBSTONE_TTL_S so the deletion can propagate, and counting those
    # told a seven-machine fleet it had joined fourteen.
    changed = len(changes) if isinstance(changes, (list, tuple, set)) else int(changes or 0)
    return f"joined: {len(inv.live(incoming))} machine(s) known, {changed} changed"
