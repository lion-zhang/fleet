"""Waiting rules shared by everything that backs off or measures an age. No imports of
fleet's own, so any module may use them."""

from __future__ import annotations

import time


def backoff(base_s: float, failures: int, cap_s: float) -> float:
    """How long to leave something that has failed `failures` times in a row: `base_s`
    after the first, doubling per failure after that, never past `cap_s` (when `cap_s`
    is 0, never past `base_s`). One rule for everything that backs off -- probing a
    machine, asking the center, retrying a grant -- where there used to be three."""
    if failures <= 1 or cap_s <= 0:
        return base_s
    return max(base_s, min(cap_s, base_s * 2 ** min(failures - 1, 20)))


def since(ts: float, now: float | None = None) -> float | None:
    """Seconds since `ts`, or None when `ts` is in the future.

    A time in the future means this clock went back (a restored VM, a reset RTC). Every
    caller treats None as "long ago" -- due, stale -- because the other answer, "not yet",
    froze fleet until the clock caught up: nothing re-measured, nothing synced, nothing
    retried.
    """
    elapsed = (time.time() if now is None else now) - ts
    return None if elapsed < 0 else elapsed
