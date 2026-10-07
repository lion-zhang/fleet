"""Expose fleet to agents that speak MCP rather than read a file.

The MCP layer of docs/design/layers.md: it runs the core (the `fleet` CLI) for agents that
cannot run commands, and is never a second implementation of it.

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

The surface is what a person may ask an agent to do by conversation: read the fleet, run
work, add and invite machines, grant and revoke, tag. Writes go through the CLI, so they
take their turn in the same queue as everyone else's. What is missing is missing for a
reason -- `top` needs a terminal to be worth anything, and the irreversible commands
(`rm`, `center --dissolve`, handing the role over) are the ones the skill reserves for a
person.
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
    from .agents import fleet_executable

    return fleet_executable()


_ANSI = None


def _clean(text: str) -> str:
    global _ANSI
    import re

    _ANSI = _ANSI or re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
    return _ANSI.sub("", text or "").strip()


def _exec(args: list[str]):
    """Run one fleet command. Never on this server's own stdin: that is the protocol
    pipe, and a child reading it (an ssh session, a prompt) would eat the client's
    messages."""
    import os

    # UTF-8 both ways: on Windows a child writing to a pipe uses the ANSI code page,
    # where a machine name or a ✓ is mangled or cannot be written at all.
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # In a process group of its own, so a timeout stops everything the command started
    # and not only the command. On Windows fleet.exe is uv's launcher: killing it, which
    # is all subprocess.run's timeout does, left the python it runs -- and that python's
    # ssh -- running, holding our pipes open, and run() then waited on those pipes for
    # as long as the remote command took. On POSIX the ssh outlived the timeout too.
    kw: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt" else {"start_new_session": True})
    proc = subprocess.Popen([_fleet(), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace", env=env, **kw)
    try:
        out, errout = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass                          # something escaped the tree; do not wait on it
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, errout)


def _kill_tree(proc) -> None:
    """Stop a process and every process it started."""
    import os
    import signal
    from contextlib import suppress

    if os.name == "nt":
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
    else:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with suppress(OSError):
        proc.kill()


def _run(args: list[str]) -> Any:
    """Run one fleet command and return its result, parsed when it is JSON.

    Errors come back as a value rather than an exception: an agent needs to read what
    went wrong and say so, and an MCP error frame tends to surface as "the tool is
    broken" when the honest answer is "that machine is switched off". The JSON is read
    even when the command failed -- `fleet add` reports an enrolment that did not work
    as a document and exit 1 -- and whatever fleet said on stderr comes along as
    `notes` (how many machines could not be judged, why a grant is pending).
    """
    try:
        p = _exec(args)
    except FileNotFoundError:
        return {"ok": False, "error": "fleet is not installed on this machine"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"`fleet {' '.join(args)}` exceeded {TIMEOUT_S}s"}
    out, notes = (p.stdout or "").strip(), _clean(p.stderr)[-2000:]
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"output": out[-4000:]}
    if not isinstance(data, dict):
        data = {"result": data}
    if p.returncode != 0:
        data = {**data, "ok": False, "exit_code": p.returncode}
        if not out:
            data["error"] = notes or f"exit {p.returncode}"
    if notes and "error" not in data:
        data["notes"] = notes
    return data


def build_server():
    """The MCP server, with the tools registered. `mcp` is imported here, not at the top:
    every other command should not pay for loading it."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ModuleNotFoundError as exc:     # pragma: no cover - mcp is a core dependency
        # Only an install from before 0.5, when mcp was an optional extra, gets here.
        raise McpUnavailable(
            "the MCP SDK is missing -- this fleet predates it being included; "
            "`uv tool install --reinstall agents-fleet`") from exc

    from .agents import package_version

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
            "center can fix.\n\n"
            "Ask rather than guess. Adding a machine needs how the user connects to it "
            "(an ssh command); a grant needs which machine may reach which; if the "
            "request does not say, ask -- never infer a machine from a partial name. "
            "Never ask for, type or accept a password: if a machine takes no key, offer "
            "`invite_machine` (one line the user pastes there) or the key `add_machine` "
            "returns for the user to put on it. An invite code admits a machine: give it to the "
            "user and nowhere else. Revoking, and anything that touches every machine, "
            "is the user's call: ask first. Removing a machine, dissolving the fleet and "
            "moving the center are not tools here -- tell the user the command.\n\n"
            "`run_on_machine` returns within 120 seconds; start anything longer "
            "detached (`nohup ... > log 2>&1 &`) and read the log later."
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

    @server.tool(description="Run one command on a machine and return its output and "
                             "exit code. This is how work actually gets started -- fleet "
                             "resolves the address and uses the fleet key, so never build "
                             "an ssh command by hand. Returns within 120 seconds: start "
                             "long jobs detached (nohup ... > log 2>&1 &).")
    def run_on_machine(machine: str, command: str) -> Any:
        try:
            p = _exec(["ssh", machine, "--", command])
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"still running after {TIMEOUT_S}s -- start it "
                                          "detached with nohup and read its log"}
        except FileNotFoundError:
            return {"ok": False, "error": "fleet is not installed on this machine"}
        return {"ok": p.returncode == 0, "exit_code": p.returncode,
                "stdout": (p.stdout or "")[-8000:], "stderr": _clean(p.stderr)[-2000:]}

    @server.tool(description="Add a machine to the fleet from how the user connects to "
                             "it: an ssh command such as `ssh -p 40001 root@1.2.3.4`. Ask "
                             "for it if the user did not give one. Key-based only: if the "
                             "machine accepts no key from here, this says so and returns "
                             "the key to put on it -- or use invite_machine. Never ask "
                             "for a password.")
    def add_machine(ssh_command: str, name: str | None = None,
                    tags: list[str] | None = None) -> Any:
        args = ["add", ssh_command, "--json"]
        if name:
            args += ["--name", name]
        for t in tags or []:
            args += ["--tag", t]
        result = _run(args)
        refused = result.get("enrolment") == "failed" or any(
            w in str(result.get("error", "")).lower()
            for w in ("permission denied", "auth", "password", "publickey"))
        if refused:
            # It answered and would not take our key: the way in that needs no password.
            key = _run(["center", "--pubkey"])
            result["next"] = ("the machine takes no key from here yet. Either put this "
                              "key in its ~/.ssh/authorized_keys and add it again, or "
                              "call invite_machine and give the user the line to paste "
                              "there.")
            result["center_pubkey"] = key.get("output", "")
        return result

    @server.tool(description="Let a machine join by itself: returns one line for the user "
                             "to paste on it (`install` for macOS/Linux, `install_windows` "
                             "for PowerShell; `command` where fleet is installed already). "
                             "No password anywhere. Single use and short-lived; the code "
                             "admits a machine, so give it to the user and nowhere else.")
    def invite_machine(name: str | None = None, valid_for: str = "15m") -> Any:
        return _run(["invite", *([name] if name else []), "--ttl", valid_for, "--json"])

    @server.tool(description="Label a machine, or change what it costs per hour (for the "
                             "$/HR column and burn rate), how it is reached, or which disks "
                             "to watch for free space: disk_paths means exactly these paths "
                             "from now on -- for a container or rental whose `/` is not "
                             "where the space is -- and autodetect_disks goes back. Tags are "
                             "yours; measured facts such as cuda or vram-24g come from probes.")
    def edit_machine(name: str, add_tags: list[str] | None = None,
                     remove_tags: list[str] | None = None, cost_per_hour: float | None = None,
                     ssh_command: str | None = None,
                     disk_paths: list[str] | None = None,
                     autodetect_disks: bool = False) -> Any:
        args = ["edit", name, "--json"]
        for path in disk_paths or []:
            args += ["--disk-path", path]
        if autodetect_disks:
            args += ["--clear-disk-paths"]
        for t in add_tags or []:
            args += ["--tag", t]
        for t in remove_tags or []:
            args += ["--untag", t]
        if cost_per_hour is not None:
            args += ["--cost", str(cost_per_hour)]
        if ssh_command:
            args += ["--ssh", ssh_command]
        return _run(args)

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

    @server.tool(description="Let one machine reach another over ssh, as the account the "
                             "machine is reached as unless `user` names another. Applied on "
                             "the spot; a machine that is off stays pending, and the center "
                             "applies it once the machine is back. "
                             "Only the center can do this. Ask before calling it: access "
                             "is the user's decision.")
    def grant_access(machine: str, may_be_reached_by: str, user: str | None = None) -> Any:
        return _run(["access", machine, "--allow", may_be_reached_by,
                     *(["--user", user] if user else []), "--json"])

    @server.tool(description="Take that access away again, for every account unless "
                             "`user` names one, applied on the spot. Ask before calling it: "
                             "the other machine loses its way in.")
    def revoke_access(machine: str, reached_by: str, user: str | None = None) -> Any:
        return _run(["access", machine, "--deny", reached_by,
                     *(["--user", user] if user else []), "--json"])

    @server.tool(description="Bring this machine up to date. On the center: apply "
                             "pending access changes and share the inventory. On a member: "
                             "fetch a fresh copy from the center. Safe to repeat.")
    def sync_fleet() -> Any:
        return _run(["sync", "--json"])

    return server


def serve() -> None:
    """Run the server on stdio, which is how desktop clients launch it."""
    build_server().run("stdio")
