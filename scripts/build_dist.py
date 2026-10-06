"""Write every file fleet ships for installing it from inside an agent.

    uv run python scripts/build_dist.py

One source, many formats: the skill text `fleet setup` writes (src/fleet/agents/usage.py)
and the version in pyproject.toml become

    skills/fleet/SKILL.md            Claude Code plugin, Gemini CLI extension,
                                     `npx skills add lion-zhang/fleet`
    hooks/hooks.json                 Claude Code plugin: says how to install when missing
    .claude-plugin/plugin.json       Claude Code plugin manifest
    .claude-plugin/marketplace.json  this repository as its own plugin marketplace
    gemini-extension.json            Gemini CLI extension: the MCP server
    server.json                      the MCP Registry entry
    mcpb/manifest.json, pyproject.toml, server.py   the Claude Desktop bundle

Generated, never hand-edited: tests/test_dist.py fails when a committed copy differs
from what this writes. Re-run it after changing the skill text or the version.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from fleet import links  # noqa: E402
from fleet.agents.usage import dist_skill_text  # noqa: E402

NAME = "fleet"
MCP_NAME = "io.github.lion-zhang/fleet"
# The registry caps a description at 100 characters; one sentence serves everywhere.
SHORT = "Every machine you have, for your coding agent: live GPU, VRAM, RAM and disk, and SSH."
AUTHOR = {"name": "Lin Zhang", "url": "https://github.com/lion-zhang"}
KEYWORDS = ["gpu", "inventory", "ssh", "remote", "compute", "mcp", "homelab", "mlops"]
# What a client runs. `uvx` builds the environment on first use and caches it.
LAUNCH = {"command": "uvx", "args": [links.PACKAGE, "mcp"]}
# Said by the plugin at session start, only when fleet is missing; never installs.
HOOK = ("command -v fleet >/dev/null 2>&1 || [ -x \"$HOME/.local/bin/fleet\" ] || "
        "echo 'The fleet plugin is enabled but the fleet command is not installed on this "
        "machine. If the user asks about their machines, GPUs or remote compute, tell "
        f"them it installs with: curl -LsSf {links.INSTALL_SH} | sh   (Windows: "
        f"irm {links.INSTALL_PS1} | iex, or anywhere: uv tool install {links.PACKAGE}). "
        "Do not install it unasked: it makes this machine the center of a new fleet.'")


def version() -> str:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]


def _json(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def outputs() -> dict[str, str]:
    """Every generated file, by path relative to the repository root."""
    v = version()
    out = {
        "skills/fleet/SKILL.md": dist_skill_text(),
        "hooks/hooks.json": _json({
            "description": "Say how to install fleet when it is missing; never install it.",
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": HOOK}]}]},
        }),
        ".claude-plugin/plugin.json": _json({
            "name": NAME, "version": v, "description": SHORT, "author": AUTHOR,
            "homepage": links.REPO, "repository": links.REPO, "license": "MIT",
            "keywords": KEYWORDS,
        }),
        ".claude-plugin/marketplace.json": _json({
            "name": NAME, "owner": AUTHOR,
            "metadata": {"description": "fleet, as a Claude Code plugin."},
            "plugins": [{"name": NAME, "source": "./", "description": SHORT, "version": v,
                         "author": AUTHOR, "homepage": links.REPO, "license": "MIT",
                         "keywords": KEYWORDS, "category": "devops"}],
        }),
        "gemini-extension.json": _json({
            "name": NAME, "version": v, "description": SHORT,
            # No context file: Gemini CLI loads the extension's skills/ on its own, and a
            # skill costs a session nothing until it is used, where GEMINI.md is read
            # into every one.
            "mcpServers": {NAME: LAUNCH},
        }),
        "server.json": _json({
            "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
            "name": MCP_NAME, "title": "fleet", "description": SHORT, "version": v,
            "repository": {"url": links.REPO, "source": "github"}, "websiteUrl": links.REPO,
            "packages": [{"registryType": "pypi", "identifier": links.PACKAGE, "version": v,
                          "runtimeHint": "uvx", "transport": {"type": "stdio"},
                          "packageArguments": [{"type": "positional", "value": "mcp"}]}],
        }),
        # The Claude Desktop bundle: the uv server type, so the host provides Python and
        # installs the pinned release itself. Nothing of fleet is bundled but these.
        "mcpb/manifest.json": _json({
            "manifest_version": "0.4", "name": NAME, "display_name": "fleet", "version": v,
            "description": SHORT,
            "long_description": ("Lets Claude see every machine you have -- which GPUs are "
                                 "free, how much VRAM, RAM and disk -- and run commands on "
                                 "them over SSH. Runs the fleet CLI, which keeps its state "
                                 "on this machine; the first use makes this machine the "
                                 "center of a new fleet."),
            "author": AUTHOR, "homepage": links.REPO, "documentation": links.REPO,
            "support": f"{links.REPO}/issues",
            "repository": {"type": "git", "url": links.REPO},
            "server": {"type": "uv", "entry_point": "server.py",
                       "mcp_config": {"command": "uv", "args": [
                           "run", "--directory", "${__dirname}", "server.py"]}},
            "compatibility": {"platforms": ["darwin", "linux", "win32"],
                              "runtimes": {"python": ">=3.12"}},
            "keywords": KEYWORDS, "license": "MIT",
        }),
        "mcpb/pyproject.toml": (
            "# Generated by scripts/build_dist.py. What Claude Desktop installs for the bundle.\n"
            "[project]\nname = \"fleet-mcpb\"\n"
            f"version = \"{v}\"\nrequires-python = \">=3.12\"\n"
            f"dependencies = [\"{links.PACKAGE}=={v}\"]\n"),
        "mcpb/server.py": (
            "\"\"\"Claude Desktop's entry point: fleet's MCP server, over stdio.\n\n"
            "Generated by scripts/build_dist.py.\n\"\"\"\n\n"
            "import sys\n\nfrom fleet.cli import main\n\n"
            "sys.argv = [\"fleet\", \"mcp\"]\nmain()\n"),
    }
    return out


def main() -> None:
    for rel, text in outputs().items():
        path = REPO / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            print(f"wrote {rel}")


if __name__ == "__main__":
    main()
