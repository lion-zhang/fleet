"""The one exception an operation may raise.

`typer.Exit` carries an exit code, which is a fact about a terminal -- so an operation
that raised it could only ever be run from one. That is the single line of code standing
between these operations and being callable from the HTTP center and from MCP, which is
why replacing it is the part of this reorganisation that is not merely moving files.

The exit code is kept, because it is information the CLI genuinely wants and the caller
is free to ignore: 2 for "you asked for something that cannot work", 1 for "it went
wrong on the way".
"""

from __future__ import annotations


class FleetError(RuntimeError):
    """An operation could not finish. `code` is advice for a caller that has exit codes."""

    def __init__(self, message: str, *, code: int = 1, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail        # the far side's own words, when there are any
