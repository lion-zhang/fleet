"""How long the center has been quiet, said in a sentence.

This lived in `access.py`, which is the module that signs and verifies envelopes. A
function whose entire job is to phrase "7d" as a line of English is a rendering concern
wearing a crypto module's coat, and the distance mattered: read there, it looks like a
check; read here, it is obviously a caption.

Deliberately a note and never a refusal. Everything already granted keeps working with
the center switched off -- the keys are in authorized_keys and sshd enforces them without
consulting fleet at all -- so treating a quiet center as a loss of access would turn a
closed laptop into a fleet outage, which is precisely backwards.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..state.access import STALE_AFTER_S, center_last_seen


def staleness_note(cache_path: Path | None = None) -> str:
    """A line for `ls` and `top` when the center has been quiet, or ""."""
    seen = center_last_seen(cache_path)
    if not seen:
        return ""
    age = int(time.time()) - seen
    if age < STALE_AFTER_S:
        return ""
    days = age // 86400
    return (f"the center has not swept this machine for {days}d -- grants and revokes "
            "are queued until it does")
