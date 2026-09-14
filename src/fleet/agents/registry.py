"""Which agents fleet knows how to talk to, and where fleet itself lives.

A data table, deliberately. Supporting a new coding agent or a new MCP client should be
a row here and nothing else -- the moment it takes a branch in `docs.py` as well, the
next one takes two, and adding an agent stops being something anyone does casually.

The two ways of naming fleet are not interchangeable and the distinction is load-bearing:
`fleet_command()` returns the bare name, for a skill an agent will type into a shell;
`fleet_executable()` returns an absolute path, for an MCP config launched by a desktop
client that has never read a profile. Using the wrong one reports that fleet is not
installed on a machine where it plainly is.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Change:
    """One file setup would touch. `action` is what happened, or would happen."""

    target: str
    path: Path
    action: str                            # created | updated | unchanged | removed


def fleet_command() -> str:
    """The command an agent should actually type.

    A skill that says `fleet ls` is worthless if fleet is not on PATH, so fall back to
    an absolute path. The subtle case is a venv: while one is active its bin directory
    sorts first, so a plain `which` resolves to a copy the agent's shell -- which does
    not inherit that activation -- cannot see. Skip venv directories and keep walking:
    a real install further down PATH still means the bare name works everywhere.
    """
    return "fleet" if _fleet_on_path() else str(_fallback_exe())



def _fleet_on_path() -> Path | None:
    """The fleet executable found on PATH, ignoring any inside the running venv."""
    prefix = Path(sys.prefix).resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        try:
            if directory.resolve().is_relative_to(prefix):
                continue
        except OSError:                     # unreadable PATH entry; not our problem
            continue
        exe = directory / "fleet"
        if exe.is_file() and os.access(exe, os.X_OK):
            return exe
    return None



def _fallback_exe() -> Path:
    """fleet beside the interpreter running us, when PATH does not have it.

    The unresolved directory first, deliberately. In a uv tool environment bin/python3
    is a symlink into the shared interpreter install, whose bin holds no fleet at all --
    so resolving walks out of the one directory the executable is certainly in, and the
    MCP server answered "fleet is not installed on this machine" from inside its own
    install.
    """
    beside = Path(sys.executable).parent / "fleet"
    return beside if beside.exists() else Path(sys.executable).resolve().parent / "fleet"



# Hermes organises skills into categories; the docs' own example for infrastructure
# tooling is skills/devops/<name>/. fleet is inventory and remote execution, so devops.
HERMES_CATEGORY = "devops"



@dataclass(frozen=True, slots=True)
class Agent:
    """One coding agent, as data rather than a branch.

    Supporting a new one is an entry in AGENTS below. Only four things actually vary:
    where its file lives in a home directory, where it lives inside a repo, whether
    fleet owns that file outright or must merge into one the user owns, and how to tell
    the agent is installed at all. Everything else -- the text, the marker region, the
    dedupe, the uninstall -- is already shared.

    Ownership is read from the filename rather than declared: a SKILL.md is ours to
    write wholesale, anything else is the user's and gets a marked region. That rule
    predates this table and is the one thing that must never be got wrong, so it stays
    in one place.
    """

    name: str
    home: str                              # path under the home root, "/"-separated
    project: str                           # path under a repo root
    skill: str = "std"                     # frontmatter dialect when the file is a SKILL.md
    detect: str = ""                       # directory meaning "installed"; default .<name>
    legacy: tuple[str, ...] = ()           # paths we used to write and must now clean up

    @property
    def marker(self) -> str:
        return self.detect or f".{self.name}"



AGENTS = (
    Agent("claude", home=".claude/skills/fleet/SKILL.md",
          project=".claude/skills/fleet/SKILL.md"),
    # A skill, not ~/.codex/AGENTS.md. Codex grew a skills directory -- ~/.codex/skills,
    # same frontmatter as Claude Code's -- and AGENTS.md is read into every conversation
    # whether or not it is about machines. That is the reasoning the hermes entry below
    # already applies to SOUL.md; it holds here for the same reason. The old file is
    # listed as legacy so the block we left in it is taken back out.
    Agent("codex", home=".codex/skills/fleet/SKILL.md", project="AGENTS.md",
          legacy=(".codex/AGENTS.md",)),
    # Not SOUL.md: that is Hermes's system prompt, so a block there would cost tokens in
    # every conversation. Skills load only when a task needs them.
    Agent("hermes", home=f".hermes/skills/{HERMES_CATEGORY}/fleet/SKILL.md",
          project="AGENTS.md", skill="hermes"),
    # GEMINI.md belongs to the user, so it gets a marked region like AGENTS.md rather
    # than being written wholesale.
    Agent("gemini", home=".gemini/GEMINI.md", project="GEMINI.md"),
)



@dataclass(frozen=True, slots=True)
class McpClient:
    """An agent that speaks MCP instead of reading a file.

    A desktop client has no shell, so the skills above are useless to it: it needs a
    server it can launch and typed tools it can call. Registering one is a key in a JSON
    config rather than a region in markdown, which is why this is a second table and not
    another column on the first.

    The configs here belong to the user and routinely hold other servers' credentials,
    so the merge only ever adds or replaces our own key and rewrites nothing else.
    """

    name: str
    key: str                               # the object our entry goes in
    macos: str                             # path under the home directory
    windows: str = ""
    linux: str = ""

    def path(self, root: Path, platform: str) -> Path | None:
        rel = {"darwin": self.macos, "win32": self.windows}.get(platform, self.linux)
        return root.joinpath(*rel.split("/")) if rel else None



MCP_CLIENTS = (
    McpClient("claude-desktop", key="mcpServers",
              macos="Library/Application Support/Claude/claude_desktop_config.json",
              windows="AppData/Roaming/Claude/claude_desktop_config.json",
              linux=".config/Claude/claude_desktop_config.json"),
    McpClient("cursor", key="mcpServers", macos=".cursor/mcp.json",
              windows=".cursor/mcp.json", linux=".cursor/mcp.json"),
    # VS Code names the object `servers`, not `mcpServers`, which is why the key is a
    # field here rather than a constant.
    McpClient("vscode", key="servers",
              macos="Library/Application Support/Code/User/mcp.json",
              windows="AppData/Roaming/Code/User/mcp.json",
              linux=".config/Code/User/mcp.json"),
    McpClient("windsurf", key="mcpServers",
              macos=".codeium/windsurf/mcp_config.json",
              windows=".codeium/windsurf/mcp_config.json",
              linux=".codeium/windsurf/mcp_config.json"),
)



def fleet_executable() -> str:
    """An absolute path to fleet, for anything launched outside a shell.

    `fleet_command` may answer with the bare name, which is right for a skill: an agent
    types it into a shell that has read a profile. A desktop client is not a shell. On
    macOS a GUI application inherits a PATH with no ~/.local/bin in it, so the bare name
    is the one answer guaranteed to fail exactly where we cannot see it fail.
    """
    return str(_fleet_on_path() or _fallback_exe())



# The one list. cli.py validates against this rather than repeating it.
TARGETS = tuple(a.name for a in AGENTS)



BY_NAME = {a.name: a for a in AGENTS}



def package_version() -> str:
    """The installed version, never a hardcoded one, which would drift immediately."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("fleet-broker")
    except PackageNotFoundError:            # running from a source tree, not installed
        return "0.0.0"

