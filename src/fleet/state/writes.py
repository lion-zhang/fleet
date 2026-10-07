"""How fleet writes its state: atomically, and one change at a time, in order.

The core has no server process. Every `fleet ...` an agent runs is a short process, and
the state is plain files, so several of them -- Claude Code and Codex on one machine, an
MCP server, a listener -- can change the same file at the same moment. A change used to
be "load the file, change the copy, save it", and two of those at once lost one change:
both loaded the same file, and the later save erased the earlier change while both
reported success. Twelve simultaneous grants kept two.

Two rules, from docs/design/layers.md, fix that without making anyone wait on anyone:

**Reads never wait.** A file is only ever replaced whole (`atomic_write`: a unique
temporary file beside it, then a rename), so a reader always sees a complete file --
the old one or the new one, never half of either. Readers take no ticket.

**Writes queue.** A writer takes a ticket (`turn`), and when its turn comes it loads the
file *as it is now*, applies its own small change and replaces the file. The queue is
first come, first served, so a grant followed by a revoke ends revoked. A turn holds
only local file work -- milliseconds -- never a network call: the rule for callers is
network first, then queue the change. So a writer waits only for the few writes queued
before it, and nothing waits on a slow machine.

A ticket is a file in a queue directory beside the state file it guards
(`.access.yaml.queue/`), named so that names sort in arrival order, and holding the
owner's pid and process start time. A writer that died mid-turn leaves a ticket nobody
answers to; it is recognised as dead and skipped, so a crash cannot wedge the queue.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import tempfile
import threading
import time
from pathlib import Path

# A turn is local file work; anything near this long is a dead or stuck writer.
_STALE_S = 120.0
_POLL_S = (0.002, 0.05)                   # first and longest sleep while waiting


class QueueTimeout(RuntimeError):
    """The writes ahead of ours did not finish in time."""


# Turns this thread holds, so that code inside a turn which itself asks for the same turn
# (a helper that saves) goes straight through instead of queueing behind itself.
_held = threading.local()


def atomic_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Replace `path` with `text` in one step, readable only by its owner by default.

    A unique temporary file in the same directory, so two writers never share one (they
    used to share `access.tmp`, and one of them crashed renaming a file the other had
    already moved). Permissions are set before a byte is written, and the data is synced
    before the rename, so the file is never seen half-written or briefly world-readable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):          # not on Windows, where the user profile is private
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def replace_file(path: Path, text: str) -> None:
    """`atomic_write` for a file that is the user's, not fleet's: keep its permissions.

    An agent's AGENTS.md, a client's config, a skill file. Written in place, an agent
    starting up at that moment could read it half-written, and two `fleet setup` runs at
    once could interleave -- leaving fragments outside fleet's marked region that
    nothing would ever clean up. New files get the usual 0644.
    """
    try:
        mode = Path(path).stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    atomic_write(path, text, mode=mode)


def _replace(src: str, dst: Path) -> None:
    """os.replace, patient on Windows, where a reader holding the file makes it fail."""
    for attempt in range(50):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            time.sleep(0.01)


def _remove(path: Path) -> None:
    """Delete a ticket or mark, patiently on Windows.

    Windows refuses to delete a file another process has open, and every waiting writer
    opens tickets to read their owner. A ticket whose delete was refused stayed behind
    with its owner alive and numbered first -- so every later write, its owner's next
    one included, waited on it until the queue timed out (16 writers on a real Windows
    runner: all of them). A read holds the file for microseconds; retry until it is gone.
    """
    for attempt in range(500):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if os.name != "nt":
                raise
            time.sleep(0.01)
    os.unlink(path)


def _queue_dir(path: Path) -> Path:
    return Path(path).parent / f".{Path(path).name}.queue"


def _me() -> dict:
    return {"pid": os.getpid(), "started": _start_time(os.getpid())}


def _start_time(pid: int) -> float:
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:
        return 0.0


def _alive(owner: dict) -> bool:
    """Whether the process that took a ticket is still the one running under its pid."""
    pid = int(owner.get("pid") or 0)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        started = float(owner.get("started") or 0)
        # A recycled pid belongs to a process that started after the ticket was taken.
        return not started or abs(psutil.Process(pid).create_time() - started) < 1.0
    except Exception:
        return True                        # cannot tell: assume alive, age decides


# Tickets are numbered Lamport's way (the "bakery"): a writer first marks itself as
# choosing, takes one more than the highest number it can see, publishes its ticket, then
# stops choosing. A writer waits while anyone is choosing, and then for every live ticket
# numbered before its own. Without the choosing step two writers can pick their numbers
# at the same instant, each not yet seeing the other's ticket, and both go first -- which
# lost four changes in four hundred on the first version of this module.

def _owner_of(path: Path) -> dict | None:
    """The owner recorded in a ticket or choosing mark; None if it is dead or stale."""
    try:
        owner = json.loads(path.read_bytes() or b"{}")
        age = time.time() - path.stat().st_mtime
    except (OSError, ValueError):
        return {}                          # vanished or unreadable this instant: look again
    if not _alive(owner) or age > _STALE_S:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return owner


def _publish(qdir: Path, name: str) -> Path:
    """Write the owner into `name` whole: a hidden temporary file, then a rename."""
    fd, tmp = tempfile.mkstemp(dir=str(qdir), prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        json.dump(_me(), fh)
    dest = qdir / name
    os.replace(tmp, dest)
    return dest


def _number(name: str) -> int:
    try:
        return int(name.split("-", 1)[0])
    except ValueError:
        return 0


def _take_ticket(qdir: Path) -> tuple[Path, tuple[int, str]]:
    qdir.mkdir(parents=True, exist_ok=True)
    me = f"{os.getpid()}-{threading.get_ident() % 100000:05d}-{random.randrange(1 << 30):010d}"
    choosing = _publish(qdir, f"choosing-{me}")
    try:
        highest = max((_number(n) for n in os.listdir(qdir) if n[:1].isdigit()), default=0)
        number = highest + 1
        ticket = _publish(qdir, f"{number:020d}-{me}")
    finally:
        with contextlib.suppress(OSError):
            _remove(choosing)
    return ticket, (number, me)


def _must_wait(qdir: Path, mine: tuple[int, str]) -> bool:
    """Whether anyone is still choosing, or holds a live ticket numbered before ours."""
    for name in sorted(os.listdir(qdir)):
        if name.startswith("choosing-"):
            if name != f"choosing-{mine[1]}" and _owner_of(qdir / name) is not None:
                return True
        elif name[:1].isdigit():
            number, _, who = name.partition("-")
            if (int(number), who) < mine and _owner_of(qdir / name) is not None:
                return True
    return False


@contextlib.contextmanager
def turn(path: Path, *, timeout: float = 60.0):
    """Wait for this writer's turn at `path`, then hold it for the body.

    Inside, load the file, change it, and save it with `atomic_write`. Never make a
    network call inside: every writer queued behind would wait for it.
    """
    key = str(Path(path).resolve())
    held = _held.__dict__.setdefault("paths", set())
    if key in held:
        yield
        return
    qdir = _queue_dir(path)
    ticket, mine = _take_ticket(qdir)
    held.add(key)
    try:
        deadline = time.monotonic() + timeout
        sleep = _POLL_S[0]
        while _must_wait(qdir, mine):
            if time.monotonic() > deadline:
                raise QueueTimeout(f"the writes queued before this one on {Path(path).name} "
                                   f"did not finish within {timeout:.0f}s")
            time.sleep(sleep)
            sleep = min(sleep * 2, _POLL_S[1])
        yield
    finally:
        held.discard(key)
        with contextlib.suppress(OSError):
            _remove(ticket)


@contextlib.contextmanager
def try_turn(path: Path):
    """Take the turn only if it is free right now; yields whether we have it.

    For work that one process doing is enough -- refreshing from the center, say: when
    several agents read the fleet at once, one refreshes and the rest carry on with
    what is on disk, rather than queueing to repeat the same round trip.
    """
    key = str(Path(path).resolve())
    held = _held.__dict__.setdefault("paths", set())
    if key in held:
        yield True
        return
    qdir = _queue_dir(path)
    ticket, mine = _take_ticket(qdir)
    try:
        if _must_wait(qdir, mine):
            yield False
            return
        held.add(key)
        try:
            yield True
        finally:
            held.discard(key)
    finally:
        with contextlib.suppress(OSError):
            _remove(ticket)
