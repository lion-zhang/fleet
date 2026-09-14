"""The two consoles, and the glyphs.

A leaf: it imports nothing from fleet, so anything may import it and the dependency only
ever points one way. That is the whole reason it exists separately.

Operations print as they go -- sixty-odd calls across a sweep, a handover, a dissolve --
and threading a reporter object through every one of them would be ceremony for its own
sake in a tool whose primary surface is a terminal. Letting them import one leaf keeps
the layering honest without pretending the output is someone else's problem.
"""

from __future__ import annotations

import json as jsonlib
import sys
from contextlib import contextmanager, suppress

from rich.console import Console

# A Windows console encodes as cp1252 by default, which has no glyph for the marks this
# CLI leans on -- the check, the diamond that names the center, the arrow for "this
# machine", the box-drawing gutter. rich does not degrade there: it raises
# UnicodeEncodeError, so `fleet center --init` created the fleet and then died printing
# that it had. Reconfigure the streams rather than dropping the glyphs, which carry
# meaning, and which every terminal anyone actually uses renders fine.
for _stream in (sys.stdout, sys.stderr):
    if (getattr(_stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
        with suppress(Exception):       # not reconfigurable under some capture harnesses
            _stream.reconfigure(encoding="utf-8", errors="replace")

console = Console()
err = Console(stderr=True)

DOT = {"ok": "[green]●[/green]", "auth_failed": "[yellow]◐[/yellow]",
       "timeout": "[dim]○[/dim]", "refused": "[red]○[/red]", "closed": "[red]○[/red]",
       "unreachable": "[dim]○[/dim]", "host_key_mismatch": "[yellow]◐[/yellow]",
       "probe_error": "[yellow]◐[/yellow]", "unknown": "[dim]?[/dim]"}


def emit(payload, as_json: bool) -> bool:
    """Print the payload as JSON if asked. Returns whether it did, so a caller can stop."""
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))
    return as_json


@contextmanager
def chatter_to_stderr(active: bool):
    """Keep stdout to one JSON document while side-effectful work reports progress.

    Redirects the stream rather than reassigning `console.file`: rich resolves an unset
    `file` to `sys.stdout` at print time, so saving and restoring it *pins* the console
    to whichever stdout happened to be current -- under a test runner, a captured buffer
    that is dead by the next test. That fails nothing here and 46 tests elsewhere.
    """
    if not active:
        yield
        return
    import contextlib

    with contextlib.redirect_stdout(sys.stderr):
        yield
