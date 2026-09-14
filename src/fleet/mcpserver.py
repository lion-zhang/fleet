"""Expose fleet to agents that speak MCP rather than read a file.

Two kinds of agent exist and they need opposite things. A coding agent with a shell
reads the skill `fleet setup` writes and types commands. A desktop client -- Claude
Desktop, ChatGPT -- has no shell at all, so instructions are useless to it; it needs
typed tools. This module is the second half, and it is why `fleet setup` alone was never
going to reach them.

**It shells out to the CLI on purpose.** Importing fleet's internals would be faster and
would slowly grow a second surface that answers differently -- which is the failure
`view.py`'s contract exists to prevent, one layer up. `cli.py` states the same rule from
the other side: the CLI, not MCP, is the universal interface. So every tool here is a
thin wrapper over the command a human would type, and the two cannot drift apart.

The surface is deliberately the whole read side plus command execution, not a curated
subset: an agent picks what it can use. What is missing is missing for a reason --
`top` needs a terminal to be worth anything, and the irreversible commands (`rm`,
`center --dissolve`, handing the role over) are the ones fleet's own agent instructions
already reserve for a human.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

TIMEOUT_S = 120


class McpUnavailable(RuntimeError):
    """The optional dependency is not installed."""


def _fleet() -> str:
    """An absolute path to fleet. Never the bare name.

    This server is launched by a desktop client, not from a shell, so it inherits a PATH
    that has never read a profile -- on macOS a GUI application gets no ~/.local/bin at
    all. `fleet_command()` is right for a skill, where an agent types into a shell, and
    exactly wrong here: every tool would answer "fleet is not installed on this machine"
    on a machine where it plainly is.
    """
    from .setup import fleet_executable

    return fleet_executable()


def _run(args: list[str]) -> Any:
    """Run one fleet command and return what it printed, parsed if it is JSON.

    Errors come back as a value rather than an exception: an agent needs to read what
    went wrong and say so, and an MCP error frame tends to surface as "the tool is
    broken" when the honest answer is "that machine is switched off".
    """
    try:
        p = subprocess.run([_fleet(), *args], capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except FileNotFoundError:
        return {"error": "fleet is not installed on this machine"}
    except subprocess.TimeoutExpired:
        return {"error": f"`fleet {' '.join(args)}` exceeded {TIMEOUT_S}s"}
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return {"error": (p.stderr or out or f"exit {p.returncode}").strip()[-2000:],
                "exit_code": p.returncode}
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        return {"output": out}


def build_server():
    """The MCP server, with the tools registered. Imported lazily: `mcp` is an extra."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ModuleNotFoundError as exc:     # pragma: no cover - depends on the extra
        raise McpUnavailable(
            "the MCP extra is not installed -- `uv tool install --force "
            "'fleet-broker[mcp]'`, or `pip install 'mcp>=2'`") from exc

    from .setup import package_version

    server = MCPServer(
        name="fleet",
        version=package_version(),
        instructions=(
            "Your personal compute inventory. Use it to find a machine to run work on "
            "and to see what the machines are doing.\n\n"
            "Pick a machine by what it can do, not by remembering its name: "
            "`list_machines(tag=['cuda','vram-24g'])` finds NVIDIA boxes with a card of "
            "at least 24G. Size facts mean *at least*, so a machine with vram-48g also "
            "reports vram-24g. `facts` in the result is measured from the last probe; "
            "`tags` is what a human wrote. A machine with no telemetry has neither.\n\n"
            "`alerts` is blocking: a rental flagged idle is costing money now, and a "
            "device reporting unattributed VRAM is not free. `status` is not a boolean "
            "-- auth_failed means the host is up and refused our key, which only the "
            "center can fix."
        ),
    )

    @server.tool(
        description="Every machine, with what is free right now. Filter with `tag` to "
                    "pick by capability -- repeated tags must all match. Facts include "
                    "gpu, cuda, metal, multi-gpu, vram-NNg, linux/macos/windows, "
                    "x86_64/arm64, cores-NN, ram-NNg, storage-NNt, public-ip/mesh/lan, "
                    "rental/shared/appliance, plus any tag a human set.")
    def list_machines(names: list[str] | None = None, tag: list[str] | None = None,
                      online: bool = False, refresh: bool = False) -> Any:
        args = ["ls", "--json", *(names or [])]
        for t in tag or []:
            args += ["--tag", t]
        if online:
            args.append("--online")
        if refresh:
            args.append("--refresh")
        return _run(args)

    @server.tool(description="Full detail for one machine: CPU, RAM, every GPU and "
                             "mount, listening services, tags and facts. No name means "
                             "the machine fleet is running on.")
    def show_machine(name: str | None = None, refresh: bool = True) -> Any:
        return _run(["show", *( [name] if name else [] ), "--json",
                     "--refresh" if refresh else "--no-refresh"])

    @server.tool(description="Run one command on a machine and return its output. This "
                             "is how work actually gets started -- fleet resolves the "
                             "address and uses the fleet key, so never build an ssh "
                             "command by hand.")
    def run_on_machine(machine: str, command: str) -> Any:
        return _run(["ssh", machine, "--", command])

    @server.tool(description="Who may reach what, and what has not landed yet. A row "
                             "that is not `present` is a grant still in flight, not one "
                             "that failed.")
    def show_access(machine: str | None = None) -> Any:
        return _run(["access", *([machine] if machine else []), "--json"])

    @server.tool(description="Which machine decides who reaches what, whether this is "
                             "it, and when it was last heard from. A center that has "
                             "been offline for hours is normal -- it is usually a laptop "
                             "-- and everything already granted keeps working.")
    def center_status() -> Any:
        return _run(["center", "--json"])

    @server.tool(description="Let one machine reach another. Only the center can do "
                             "this, and it takes effect on the next sync. Ask before "
                             "calling it: access changes are the user's decision.")
    def grant_access(machine: str, may_be_reached_by: str, user: str = "root") -> Any:
        return _run(["access", machine, "--allow", may_be_reached_by,
                     "--user", user, "--json"])

    @server.tool(description="Apply pending access changes and collect telemetry. Only "
                             "the center can do this. Safe to repeat.")
    def sync_fleet() -> Any:
        return _run(["sync", "--json"])

    return server


def serve() -> None:
    """Run the server on stdio, which is how desktop clients launch it."""
    build_server().run("stdio")
