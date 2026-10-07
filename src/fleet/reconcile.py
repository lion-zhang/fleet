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

from .state import access as acc_mod
from .state.access import Access, AccessError
from .state.writes import atomic_write, turn
from .ssh.authkeys import sync_command
from .ssh.cmd import run as sshrun
from .ssh.cmd import Endpoint, build_argv


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


class Ledger(dict):
    """The ledger, remembering what it looked like when it was loaded.

    A sweep loads the ledger, spends minutes reaching machines, and saves. Saved whole,
    that erased every edge another fleet process recorded meanwhile -- a grant applied
    by `fleet access` came back "unknown", or worse, an install recorded as present by a
    sweep that had raced a revoke. Saving writes only the edges this copy changed, onto
    the ledger as it is now.
    """

    base: dict[str, dict]

    def __init__(self, *args, base: dict[str, dict] | None = None):
        super().__init__(*args)
        self.base = base if base is not None else {}


def load_ledger(path: Path | None = None) -> Ledger:
    """Unlike the access list, a missing ledger is fine -- it means nothing has been
    observed yet, which is true on a fresh center and is not a dangerous belief."""
    return _parse(path or acc_mod.LEDGER_PATH)


def _parse(path: Path) -> Ledger:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return Ledger()
    edges = {k: EdgeState(**v) for k, v in (raw.get("edges") or {}).items()}
    return Ledger(edges, base={k: asdict(v) for k, v in edges.items()})


def save_ledger(ledger: dict[str, EdgeState], path: Path | None = None) -> None:
    """Write the edges this copy changed onto the ledger as it is now, in turn with every
    other writer. A ledger built from nothing (no `base`) has changed every edge."""
    path = path or acc_mod.LEDGER_PATH
    base = getattr(ledger, "base", None)
    with turn(path):
        current = _parse(path) if base is not None else Ledger()
        for k, st in ledger.items():
            if base is None or base.get(k) != asdict(st):
                current[k] = st
        for k in (base or {}):
            if k not in ledger:
                current.pop(k, None)       # this copy dropped it
        # A key that should be gone and is gone is nothing left to do. Kept, every edge
        # ever revoked stayed in the ledger -- and in every sweep's plan -- for good.
        for k in [k for k, st in current.items()
                  if st.desired == "absent" and st.observed == "absent"]:
            del current[k]
        atomic_write(path, yaml.safe_dump(
            {"edges": {k: asdict(v) for k, v in current.items()}}, sort_keys=True))


def plan(acc: Access, ledger: dict[str, EdgeState]) -> dict[str, EdgeState]:
    """Fold the current list into the ledger: desired comes from the list, observed is
    whatever we last saw. Edges the list no longer contains become desired-absent rather
    than disappearing, because "this key should not be there" is work, not silence."""
    now = int(time.time())
    wanted = acc.edges()
    out = Ledger(ledger, base=getattr(ledger, "base", None))
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
        # Bytes: text mode rewrites \n to \r\n on Windows, and a CRLF-mangled script
        # half-executes -- it wrote the replacement authorized_keys to a temp file,
        # never reached the `mv`, and left the temp behind. Had that `mv` run, the file
        # would have been replaced by the block alone, losing every key we did not
        # write. The promise never to touch a line outside our own block depends on the
        # script arriving intact.
        p = sshrun(argv, input=script.encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, str(exc)
    if capture:
        # Callers that need what the far side *said*, not just whether it worked.
        return p.returncode == 0, (p.stdout or p.stderr or b"").decode(errors="replace")
    return p.returncode == 0, (p.stderr or p.stdout or b"").decode(errors="replace").strip()[-300:]


def converge_edge(acc: Access, edge: tuple[str, str, str], ep: Endpoint, *,
                  install: bool, platform: str = "posix",
                  access_path: Path | None = None) -> tuple[bool, str, bool]:
    """Make one machine match what the access list says *now*, not when we started.

    Two fleet processes can change the same edge at once -- an agent grants while
    another revokes, or a sweep planned minutes ago reaches a machine just after a
    revoke. Each edit on the machine is atomic, but the order they land in is not ours
    to choose, so a revoke could land first and the stale install after it, leaving a
    key the list says is gone. So after each edit the list is read again, and if what it
    wants changed meanwhile, the edit is made again. Whoever edits last has read the
    newest list. Returns (ok, detail, whether the key is now meant to be installed).
    """
    for _ in range(3):
        ok, detail = apply_edge(acc, edge, ep, install=install, platform=platform)
        if not ok:
            return ok, detail, install
        try:
            acc = acc_mod.load(access_path)
        except AccessError:
            return ok, detail, install     # no list here any more: nothing newer to obey
        wanted = edge in acc.edges()
        if wanted == install:
            return ok, detail, install
        install = wanted                   # it changed while we were connected
    return ok, detail, install


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
        return False, (f"no pinned key for {acc.name_of(src)} -- `fleet sync` enrols it, "
                       "or `fleet center --pubkey` if the center cannot get in")
    if _dst == acc.center and acc_mod.is_center(acc):
        # The center's own authorized_keys, edited in place. Over ssh it meant the center
        # dialling itself -- which fails, since nothing installs the center's key on the
        # center -- so the outgoing center's key, the one edge that lands here after a
        # handover, stayed on the new center for good.
        from .ssh.cmd import local_platform, local_shell_argv

        script = sync_command(acc.fleet_id, src, user=user,
                              pubkey=pinned if install else None, platform=local_platform())
        p = subprocess.run(local_shell_argv(), input=script.encode(), capture_output=True)
        return p.returncode == 0, (p.stdout + p.stderr).decode(errors="replace")
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
