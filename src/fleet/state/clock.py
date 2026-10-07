"""This machine's clock, corrected to the center's.

Timestamps decide merges -- the newer record wins whole -- and ages decide what is shown
("seen 2 min ago"). A member whose clock runs ahead stamped its edits in the future: the
center caps what it takes, but the member's own copy kept the future stamp, so it went
on preferring its own record over every change the center made to it until its clock
caught up -- hours, on a machine with no NTP. And readings relayed by the center carried
the center's time, which a member with a wrong clock showed as the wrong age.

The center puts the time it sent each signed answer in it. A member keeps the
difference here, stamps its own changes with the corrected time, and converts relayed
times into its own clock. On the center the offset is always 0.
"""

from __future__ import annotations

import time

import yaml

from ..config import STATE_DIR

OFFSET_PATH = STATE_DIR / "clock.yaml"
# Under this, the difference is network latency and rounding, not a wrong clock.
_IGNORE_S = 2


def offset() -> int:
    """Seconds to add to this machine's clock to get the center's. 0 on the center --
    including one that was a member before a handover, which still has the file."""
    from . import access

    if access.ACCESS_PATH.exists():
        return 0
    try:
        data = yaml.safe_load(OFFSET_PATH.read_text(encoding="utf-8")) or {}
        return int(data.get("offset_s") or 0)
    except (OSError, yaml.YAMLError, ValueError, TypeError, AttributeError):
        return 0


def now() -> int:
    """The time to stamp a change with: the center's clock, as best we know it."""
    return int(time.time()) + offset()


def to_local(ts: int) -> int:
    """A time on the center's clock, on this machine's."""
    return int(ts) - offset() if ts else ts


def note_center_time(sent_at: int) -> None:
    """Remember how far this clock is from the center's, from a signed answer it sent."""
    if not sent_at:
        return                     # a center from before this; leave what we know
    from .writes import atomic_write, turn

    diff = int(sent_at) - int(time.time())
    if abs(diff) <= _IGNORE_S:
        diff = 0
    if diff == offset():
        return
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with turn(OFFSET_PATH):
        atomic_write(OFFSET_PATH, yaml.safe_dump({"offset_s": diff}))
