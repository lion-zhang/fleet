"""What fleet does, as opposed to how you ask it to.

This layer exists because there was nowhere for an operation to live that was not a
command. Sweeping, enrolling, handing the centre over, dissolving a fleet -- all of it
sat inside `cli.py`, which is why `serve.py` had to import the CLI to reach it, and why
none of it could be called from MCP at all: it raised `typer.Exit`, so it only ran in a
terminal.

The rule here is one sentence: **an operation may raise `FleetError` and may print, but
it may not know what a command-line is.** `cli.py` catches `FleetError` and turns it into
an exit code; `serve.py` and `mcp.py` catch the same thing and turn it into a status or a
tool result. Three surfaces, one implementation.
"""

from __future__ import annotations

from .errors import FleetError

__all__ = ["FleetError"]
