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

import functools
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
    found = _fleet_on_path()
    if found and _found_by_a_fresh_shell():
        return "fleet"
    if not found and _ephemeral():
        return UVX
    # The one on PATH by preference: uv's shim in ~/.local/bin outlives a reinstall's
    # rebuilt environment, and reads as what it is.
    return str(found or _fallback_exe())


@functools.lru_cache(maxsize=1)
def _found_by_a_fresh_shell() -> bool:
    """Whether a shell that starts from nothing finds `fleet` by name.

    Finding it on *our* PATH is not enough. The installer puts ~/.local/bin on its own
    PATH before running setup, and agents often run commands through a plain `sh -c`
    that inherits no such thing -- on Ubuntu, root's profile never adds ~/.local/bin at
    all. A skill that says `fleet ls` there fails on its first command. So ask a login
    shell with a bare environment; when it cannot find fleet, the skill carries the full
    path, which is never wrong on the machine it was written for.
    """
    if sys.platform == "win32":
        return True                         # PATH is per-user in the registry there
    import subprocess

    env = {"HOME": str(Path.home()), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", "")}
    try:
        p = subprocess.run(["sh", "-lc", "command -v fleet"], env=env, capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and bool(p.stdout.strip())



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



# The skills folder most agents now read besides their own: Codex, Gemini CLI, Copilot
# CLI, OpenCode, Kilo and Amp all list a skill placed here. Relative to the home root.
SHARED_SKILL = ".agents/skills/fleet/SKILL.md"


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
    # Where the agent keeps its home on Windows, when that is not the POSIX dot-dir:
    # (its home directory, the file within it). Hermes is the case: it reads
    # %LOCALAPPDATA%\hermes there, and a skill in ~/.hermes was invisible to it.
    windows: tuple[str, str] | None = None
    env: str = ""                          # variable naming the agent's home, if it has one
    # Skill files fleet used to write for this agent somewhere else. Ours outright, so
    # they are removed once the skill is where the agent should read it.
    moved: tuple[str, ...] = ()

    @property
    def marker(self) -> str:
        return self.detect or f".{self.name}"

    def _within(self) -> str:
        """The file's path inside the agent's own home directory."""
        return self.home.split("/", 1)[1]

    def home_dir(self, root: Path, platform: str = "") -> Path:
        """The directory this agent treats as its home, on this machine.

        In order: the agent's own variable, when it names one and we are writing to the
        real home; on Windows, the directory it uses there -- unless only the dot-dir
        exists, which is an older install of it; otherwise the dot-dir.
        """
        platform = platform or sys.platform
        if self.env and root == Path.home() and os.environ.get(self.env):
            return Path(os.environ[self.env])
        dot = root / self.marker
        if platform == "win32" and self.windows:
            native = root.joinpath(*self.windows[0].split("/"))
            if native.is_dir() or not dot.is_dir():
                return native
        return dot

    def home_path(self, root: Path, platform: str = "") -> Path:
        if not self.windows and not self.env:
            return root.joinpath(*self.home.split("/"))
        return self.home_dir(root, platform).joinpath(*self._within().split("/"))

    def stray_paths(self, root: Path, platform: str = "") -> list[Path]:
        """Where fleet may have written this agent's file before, other than where it
        belongs now -- the copy the agent cannot see, and must not see twice."""
        here = self.home_path(root, platform)
        moved = [root.joinpath(*m.split("/")) for m in self.moved]
        if not self.windows and not self.env:
            return [m for m in moved if m != here]
        candidates = [root.joinpath(*self.home.split("/")), *moved]
        if self.windows:
            candidates.append(root.joinpath(*self.windows[0].split("/"),
                                            *self._within().split("/")))
        return [c for c in candidates if c != here]



AGENTS = (
    Agent("claude", home=".claude/skills/fleet/SKILL.md",
          project=".claude/skills/fleet/SKILL.md"),
    # A skill, not ~/.codex/AGENTS.md. Codex grew a skills directory -- ~/.codex/skills,
    # same frontmatter as Claude Code's -- and AGENTS.md is read into every conversation
    # whether or not it is about machines. That is the reasoning the hermes entry below
    # already applies to SOUL.md; it holds here for the same reason. The old file is
    # listed as legacy so the block we left in it is taken back out.
    # Now the shared ~/.agents/skills, which Codex reads beside ~/.codex/skills -- and
    # reads both of without de-duplicating, so a copy in each was fleet twice. Checked
    # with `codex debug prompt-input`, which shows what the model is given.
    Agent("codex", home=f"{SHARED_SKILL}", project="AGENTS.md",
          legacy=(".codex/AGENTS.md",), moved=(".codex/skills/fleet/SKILL.md",)),
    # Not SOUL.md: that is Hermes's system prompt, so a block there would cost tokens in
    # every conversation. Skills load only when a task needs them.
    # Its home is %LOCALAPPDATA%\hermes on Windows, and HERMES_HOME when that is set.
    # Found by a Hermes agent on a real Windows machine: the skill sat in
    # ~\.hermes\skills\devops, the scanner indexed only the active home, and fleet was
    # simply not there as far as it could tell.
    Agent("hermes", home=f".hermes/skills/{HERMES_CATEGORY}/fleet/SKILL.md",
          project="AGENTS.md", skill="hermes",
          windows=("AppData/Local/hermes", f"skills/{HERMES_CATEGORY}/fleet/SKILL.md"),
          env="HERMES_HOME"),
    # The shared skill, not a region in ~/.gemini/GEMINI.md: that file is read into every
    # session, and Gemini CLI also reads ~/.agents/skills -- so with Codex writing the
    # skill there, Gemini was given fleet twice. The region is taken back out. In a repo
    # it is still a marked region in GEMINI.md.
    Agent("gemini", home=f"{SHARED_SKILL}", project="GEMINI.md",
          legacy=(".gemini/GEMINI.md",)),
    # Agents that read only the shared folder. Each was checked against the real CLI
    # (`copilot skill list`, `opencode debug skill`, `kilo debug skill`, Amp's settings
    # reference): all list a skill placed in ~/.agents/skills. One file serves them all;
    # `fleet setup` writes it once however many of them are installed.
    Agent("copilot", home=f"{SHARED_SKILL}", project=".github/skills/fleet/SKILL.md"),
    Agent("opencode", home=f"{SHARED_SKILL}", project=f"{SHARED_SKILL}",
          detect=".config/opencode"),
    Agent("kilo", home=f"{SHARED_SKILL}", project=f"{SHARED_SKILL}", detect=".config/kilo"),
    Agent("amp", home=f"{SHARED_SKILL}", project=f"{SHARED_SKILL}", detect=".config/amp"),
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



# What a skill or a client config says when fleet runs from a throwaway `uvx` environment
# and is installed nowhere: the launcher itself, which builds that environment again.
UVX = "uvx agents-fleet"


def _ephemeral() -> bool:
    """Whether we run from an environment `uvx` built in its cache, not an install.

    `uvx agents-fleet mcp` is how the Gemini extension, the MCP registry and the one-click
    buttons launch fleet. Writing that environment's path into a skill or a config would
    point it at a directory uv may delete whenever its cache is cleaned.
    """
    return any(part.startswith("archive-v") for part in Path(sys.prefix).parts)


def config_command() -> str:
    """What to write into an MCP client's config: `fleet_executable`, unless fleet is
    installed nowhere and runs from `uvx`, in which case the launcher (see `UVX`)."""
    if not _fleet_on_path() and _ephemeral():
        return UVX
    return fleet_executable()


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
    # The distribution was `fleet-broker` until 0.5, then `agent-fleet`; a machine
    # mid-update can still have only one of those installed.
    for dist in ("agents-fleet", "agent-fleet", "fleet-broker"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0.0.0"                          # running from a source tree, not installed

