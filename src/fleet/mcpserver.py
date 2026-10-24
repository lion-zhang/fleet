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

The surface is what a person may ask an agent to do by conversation: read the fleet, its
jobs and models, run work, add machines, say why one refuses, tag, record folders. Writes
go through the CLI, so they take their turn in the same queue as everyone else's. What is
missing is missing for a reason -- `top` needs a terminal to be worth anything, and `rm`
is the one the skill reserves for a person.
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


# Commands that do a lot over the network get longer: adding a machine probes it and
# checks for a clone; doctor dials every failing machine.
SLOW_TIMEOUT_S = 300


def _exec(args: list[str], timeout: float | None = None):
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
        out, errout = proc.communicate(timeout=timeout or TIMEOUT_S)
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


def _run(args: list[str], timeout: float | None = None) -> Any:
    """Run one fleet command and return its result, parsed when it is JSON.

    Errors come back as a value rather than an exception: an agent needs to read what
    went wrong and say so, and an MCP error frame tends to surface as "the tool is
    broken" when the honest answer is "that machine is switched off". The JSON is read
    even when the command failed -- `fleet add` reports a machine that refused us as a
    document, with the fix, and exit 1 -- and whatever fleet said on stderr comes along
    as `notes` (how many machines could not be judged).
    """
    try:
        p = _exec(args, timeout)
    except FileNotFoundError:
        return {"ok": False, "error": "fleet is not installed on this machine"}
    except subprocess.TimeoutExpired:
        # Stopped part way, so part of it may have happened: say so, and how to look.
        return {"ok": False, "error": f"`fleet {' '.join(args)}` exceeded "
                                      f"{int(timeout or TIMEOUT_S)}s and was stopped; part "
                                      "of it may already have been applied -- check with "
                                      "list_machines or show_access before trying again"}
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
            "Every machine the user has: what is free on it, what is running on its "
            "GPUs, which models its servers serve, its software, and where it keeps "
            "models and data. Use it to find a machine to run work on, and to use what "
            "is already there rather than downloading or installing it again.\n\n"
            "Pick a machine by what it can do, not by remembering its name: "
            "`list_machines(tag=['cuda','vram-24g'])` finds NVIDIA boxes with a card of "
            "at least 24G. Size facts mean *at least*, so a machine with vram-48g also "
            "reports vram-24g. `facts` in the result is measured from the last probe; "
            "`tags` is what a human wrote. A machine with no telemetry has neither.\n\n"
            "`alerts` is blocking: a rental flagged idle is costing money now, and a "
            "device reporting unattributed VRAM is not free. A GPU with `jobs` is "
            "someone's: ask before using it. `status` is not a boolean -- auth_failed "
            "means the host is up and refused us; `doctor_machine` says what the user "
            "does about it.\n\n"
            "Ask rather than guess. Adding a machine needs how the user connects to it "
            "(an ssh command, or a name from discover_machines); if the request does not "
            "say, ask -- never infer a machine from a partial name. fleet manages no "
            "access: it connects as the user does. Never ask for, type or accept a "
            "password; if a machine refuses, give the user the fix doctor_machine "
            "returns. Anything that touches every machine is the user's call: ask "
            "first. Removing a machine is not a tool here -- tell the user the "
            "command.\n\n"
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

    @server.tool(description="Full detail for one machine: CPU, RAM, every GPU and its "
                             "jobs, mounts, listening services, the models its servers "
                             "serve, its software (cuda, python, and each Python "
                             "environment's torch/vllm/mlx/... versions), the folders it "
                             "keeps models, datasets and media in, tags and facts. No "
                             "name means the machine fleet is running on.")
    def show_machine(name: str | None = None, refresh: bool = True) -> Any:
        return _run(["show", *( [name] if name else [] ), "--json",
                     "--refresh" if refresh else "--no-refresh"])

    @server.tool(description="Every job on every GPU across the fleet -- who runs it, "
                             "how much VRAM, for how long, the command -- and which GPUs "
                             "are idle. A GPU with jobs is someone's: ask before using it.")
    def list_gpu_jobs(names: list[str] | None = None) -> Any:
        return _run(["ps", "--json", *(names or [])])

    @server.tool(description="Every model being served across the fleet: machine, server, "
                             "url, and for a server that only listens on its own machine "
                             "a `tunnel` command to reach it; plus the folders each "
                             "machine keeps models in. Check this before downloading a "
                             "model: one already served or on disk need not be fetched.")
    def list_models(names: list[str] | None = None) -> Any:
        return _run(["models", "--json", *(names or [])])

    @server.tool(description="Record where a machine keeps things, in its own "
                             "machine.yaml: kind is models, datasets, media, scratch, envs "
                             "or one of your own; paths replace that kind's folders, and "
                             "an empty list clears it. Ask the user where before "
                             "recording a folder they did not name.")
    def set_machine_paths(machine: str, kind: str, paths: list[str]) -> Any:
        args = ["edit", machine, "--json"]
        for p in paths or [""]:
            args += ["--path", f"{kind}={p}"]
        return _run(args)

    @server.tool(description="Run one command on a machine and return its output and "
                             "exit code. This is how work actually gets started -- fleet "
                             "resolves the address and connects as the user does, so never build "
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

    @server.tool(description="Add a machine from how the user connects to it: an ssh "
                             "command such as `ssh -p 40001 root@1.2.3.4`, or a name from "
                             "discover_machines. Ask for it if the user did not give one. "
                             "If the machine refuses, the result has `why` and `fix` -- the "
                             "one thing the user does to let fleet in. Never ask for a "
                             "password.")
    def add_machine(ssh_command: str, name: str | None = None,
                    tags: list[str] | None = None, alias: str | None = None,
                    kind: str | None = None) -> Any:
        """`kind` only when the user says what it is: permanent, rental, shared,
        appliance or mobile. fleet picks one itself otherwise."""
        args = ["add", ssh_command, "--json"]
        if name:
            args += ["--name", name]
        if alias:
            args += ["--alias", alias]
        if kind:
            args += ["--kind", kind]
        for t in tags or []:
            args += ["--tag", t]
        # A refusal comes back with `why` and `fix`: the one thing the user does to let
        # fleet in (put their key on it, add it to the provider, a Tailscale SSH rule).
        return _run(args, SLOW_TIMEOUT_S)

    @server.tool(description="Machines the user can already reach that are not added yet: "
                             "the Host entries of their ~/.ssh/config, and the machines on "
                             "their tailnet. Offer the ones that look relevant; add one "
                             "with add_machine(ssh_command=its name) once the user agrees.")
    def discover_machines() -> Any:
        return _run(["ls", "--discover", "--json"])

    @server.tool(description="Why a machine will not let fleet in, and the exact fix for "
                             "it: the key line to run there, the provider account to add "
                             "the key to, or the Tailscale SSH rule. Changes nothing; tell "
                             "the user what it says -- access is theirs to grant.")
    def doctor_machine(name: str | None = None) -> Any:
        return _run(["doctor", *([name] if name else []), "--json"], SLOW_TIMEOUT_S)

    @server.tool(description="Label a machine, or change what it costs per hour (for the "
                             "$/HR column and burn rate), how it is reached, or which disks "
                             "to watch for free space: disk_paths means exactly these paths "
                             "from now on -- for a container or rental whose `/` is not "
                             "where the space is -- and autodetect_disks goes back. rename "
                             "gives it a new name, alias a short one (empty clears it). Tags "
                             "are yours; measured facts such as cuda or vram-24g come from "
                             "probes.")
    def edit_machine(name: str, add_tags: list[str] | None = None,
                     remove_tags: list[str] | None = None, cost_per_hour: float | None = None,
                     ssh_command: str | None = None,
                     disk_paths: list[str] | None = None,
                     autodetect_disks: bool = False, rename: str | None = None,
                     alias: str | None = None) -> Any:
        args = ["edit", name, "--json"]
        if rename:
            args += ["--name", rename]
        if alias is not None:
            args += ["--alias", alias]
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

    return server


def serve() -> None:
    """Run the server on stdio, which is how desktop clients launch it."""
    build_server().run("stdio")
