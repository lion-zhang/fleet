"""Turning what a person typed into what the access list knows.

The list is keyed on key fingerprints and carries a name only to render them, so it has
no idea aliases exist. Translating at the edge is cheaper than teaching it a second
naming scheme it would then have to keep in step through every rename.
"""

from __future__ import annotations

from ..state import inventory as inv


def canonical(token: str) -> str:
    """An alias becomes the name the access list knows. Anything else passes through."""
    if not token:
        return token
    dev = inv.find_exact(inv.load(), token)
    return dev.name if dev else token
