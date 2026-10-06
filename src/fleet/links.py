"""Where fleet is published, in one place: the installers, the docs and `fleet invite`
all name the same URLs."""

from __future__ import annotations

PACKAGE = "agent-fleet"
REPO = "https://github.com/lion-zhang/fleet"
RAW = "https://raw.githubusercontent.com/lion-zhang/fleet/main"
INSTALL_SH = f"{RAW}/install.sh"
INSTALL_PS1 = f"{RAW}/install.ps1"


def install_line(join_code: str = "") -> str:
    """The one line that installs fleet on macOS/Linux; with a code, as a member."""
    tail = f" -s -- --join {join_code}" if join_code else ""
    return f"curl -LsSf {INSTALL_SH} | sh{tail}"


def install_line_windows(join_code: str = "") -> str:
    """The same for Windows. `irm | iex` takes no arguments, so a code rides an
    environment variable the script reads -- and that line is PowerShell itself: wrapped
    in `powershell -c "..."` and typed at a PowerShell prompt, the outer shell would
    expand `$env:FLEET_JOIN` to nothing before the inner one ever saw it."""
    if join_code:
        return f"$env:FLEET_JOIN='{join_code}'; irm {INSTALL_PS1} | iex"
    return f'powershell -ExecutionPolicy ByPass -c "irm {INSTALL_PS1} | iex"'
