"""Making authorized_keys on every machine match the access list.

The list says what should be true; this makes it so, and records what it observed. The
two are kept apart on purpose: a desired/observed pair is idempotent and self-healing,
where a work queue can be lost, replayed, or drained halfway and leave no trace of which.

Everything here is failure-tolerant by design. A device that is off is not an error, it
is an edge that has not converged yet -- and saying so, with an age and a retry count, is
the whole difference between this and a tool that reports a revoke as done when the key
is still sitting on the target.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import access as acc_mod
from .access import Access, AccessError
from .authkeys import sync_command
from .sshcmd import Endpoint, build_argv


@dataclass(slots=True)
class EdgeState:
    """What we want, what we last saw, and why it is not that yet."""

    desired: str = "present"          # present | absent
    observed: str = "unknown"         # present | absent | unknown
    attempts: int = 0
    last_attempt_at: int = 0
    last_error: str = ""
    pending_since: int = 0
    # Which device this edge points at, recorded when the edge is first planned.
    # Without it, dropping a machine from the access list would lose the binding needed
    # to *undo* its grants -- so the key would stay installed, permanently, and the
    # ledger could only report that it had no idea where to go.
    dst_device: str = ""

    @property
    def converged(self) -> bool:
        return self.desired == self.observed


def _key(edge: tuple[str, str, str]) -> str:
    return ">".join(edge)


def load_ledger(path: Path | None = None) -> dict[str, EdgeState]:
    """Unlike the access list, a missing ledger is fine -- it means nothing has been
    observed yet, which is true on a fresh center and is not a dangerous belief."""
    path = path or acc_mod.LEDGER_PATH
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {k: EdgeState(**v) for k, v in (raw.get("edges") or {}).items()}


def save_ledger(ledger: dict[str, EdgeState], path: Path | None = None) -> None:
    path = path or acc_mod.LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(
        {"edges": {k: asdict(v) for k, v in ledger.items()}}, sort_keys=True))
    os.replace(tmp, path)


def plan(acc: Access, ledger: dict[str, EdgeState]) -> dict[str, EdgeState]:
    """Fold the current list into the ledger: desired comes from the list, observed is
    whatever we last saw. Edges the list no longer contains become desired-absent rather
    than disappearing, because "this key should not be there" is work, not silence."""
    now = int(time.time())
    wanted = acc.edges()
    out = dict(ledger)
    for edge in wanted:
        st = out.setdefault(_key(edge), EdgeState(pending_since=now))
        if st.desired != "present":
            st.desired, st.pending_since = "present", now
        # refreshed while we still know it, so a later revoke does not need the pin
        if device := (acc.keys.get(edge[1]) or {}).get("device_id"):
            st.dst_device = device
    for k, st in out.items():
        if tuple(k.split(">")) not in wanted and st.desired != "absent":
            st.desired, st.pending_since = "absent", now
    return out


def _remote(ep: Endpoint, script: str, *, platform: str = "posix",
            timeout: int = 30, capture: bool = False):
    """Run one authorized_keys edit.

    `multiplex=False` is not an optimisation, it is a correctness requirement:
    ControlMaster paths are hashed per host, so the probe fan-out and this edit would
    share one master -- and `runner._kill` kills the whole process group on a probe
    timeout, which could tear the connection down mid-write.
    """
    remote = ("powershell -NoProfile -Command -" if platform == "windows"
              else "sh -s")
    argv = build_argv(ep, remote=remote, multiplex=False, connect_timeout=10)
    try:
        p = subprocess.run(argv, input=script, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, str(exc)
    if capture:
        # Callers that need what the far side *said*, not just whether it worked.
        return p.returncode == 0, (p.stdout or p.stderr or "")
    return p.returncode == 0, (p.stderr or p.stdout or "").strip()[-300:]


def apply_edge(acc: Access, edge: tuple[str, str, str], ep: Endpoint, *,
               install: bool, platform: str = "posix") -> tuple[bool, str]:
    """Put one machine's key on another, or take it off.

    Only ever a *pinned* key: `Device.pubkey` rides the ordinary merge, so a peer with a
    fast clock could overwrite it and have us install their key where the owner's
    belonged.
    """
    src, _dst, user = edge
    pinned = (acc.keys.get(src) or {}).get("pubkey", "")
    if install and not pinned:
        return False, f"no pinned key for {acc.name_of(src)} -- enroll it first"
    script = sync_command(acc.fleet_id, src, user=user,
                          pubkey=pinned if install else None, platform=platform)
    return _remote(ep, script, platform=platform)


def refuses_to_run(acc: Access, ledger: dict[str, EdgeState], *,
                   dissolving: bool = False) -> str:
    """A guard against the one shape that is always a bug.

    Wanting no edges at all, while having observed some, means something upstream
    returned empty -- an unreadable file, a half-written save -- and acting on it would
    strip every key fleet placed, everywhere, in one pass. There is no legitimate reason
    to reach that state in a single step: revoking is done edge by edge.
    """
    if dissolving:
        # Dissolving is the one time "remove everything" is the intent rather than a
        # symptom. It is asked for explicitly, by name, on the center, after a prompt --
        # which is precisely what this guard cannot distinguish on its own.
        return ""
    if acc.edges():
        return ""
    if any(st.observed == "present" for st in ledger.values()):
        return ("the access list is empty but keys are installed -- refusing to remove "
                "everything at once. If that is really what you want, revoke them "
                "individually.")
    return ""
