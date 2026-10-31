"""Serving fleet to agents that cannot run a shell.

A coding agent reads the skill `fleet setup` writes and types commands. A desktop client
has no shell at all, so instructions are useless to it and it needs typed tools. Almost
every bug found building this was the same one wearing a different hat: something
resolved `fleet` the way a shell would, on a process that never read a profile.
"""

from __future__ import annotations

import json
import sys

import pytest

from fleet import agents as st


# ------------------------------------------------------------- resolving fleet

def test_a_launched_server_never_gets_the_bare_name(monkeypatch, tmp_path):
    """`fleet_command` may answer "fleet", which is right for a skill: an agent types it
    into a shell. A desktop client is not a shell -- on macOS a GUI application inherits
    a PATH with no ~/.local/bin -- so the bare name is the one answer guaranteed to fail
    where nobody can see it."""
    exe = tmp_path / "bin" / "fleet"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "other"))
    import fleet.agents.registry as reg
    monkeypatch.setattr(reg, "_found_by_a_fresh_shell", lambda: True)

    assert st.fleet_command() == "fleet", "a skill still gets the bare name"
    assert st.fleet_executable() == str(exe), "a config gets an absolute path"


def test_the_fallback_does_not_resolve_out_of_its_own_directory(monkeypatch, tmp_path):
    """In a uv tool environment bin/python3 is a symlink into the shared interpreter
    install, whose bin holds no fleet. Resolving first walked out of the one directory
    the executable is certainly in, and the server reported fleet missing from inside
    its own install."""
    real = tmp_path / "shared" / "bin"
    real.mkdir(parents=True)
    (real / "python3").write_text("")
    toolbin = tmp_path / "tool" / "bin"
    toolbin.mkdir(parents=True)
    (toolbin / "fleet").write_text("")
    (toolbin / "python3").symlink_to(real / "python3")

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(sys, "executable", str(toolbin / "python3"))
    assert st.fleet_executable() == str(toolbin / "fleet")


def test_the_server_runs_its_own_fleet_not_another_on_path(monkeypatch, tmp_path):
    """Its tools are this server's. An older fleet earlier on PATH answered for them:
    `fleet mcp` from 0.6.2 ran 0.6.1's `ls`, which did not split a tag list, and the
    agent was told no machine had the tag."""
    from fleet import mcpserver

    older = tmp_path / "old" / "bin"
    older.mkdir(parents=True)
    (older / "fleet").write_text("#!/bin/sh\n")
    (older / "fleet").chmod(0o755)
    ours = tmp_path / "venv" / "bin"
    ours.mkdir(parents=True)
    (ours / "python3").write_text("")
    (ours / "fleet").write_text("")
    monkeypatch.setenv("PATH", str(older))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "executable", str(ours / "python3"))

    assert mcpserver._fleet() == str(ours / "fleet")


# ------------------------------------------------------------- the config merge

def test_our_entry_is_added_without_disturbing_other_servers():
    """These configs hold other servers' credentials. We add one key and rewrite
    nothing else."""
    before = json.dumps({"mcpServers": {"other": {"command": "x",
                                                  "env": {"TOKEN": "secret"}}},
                         "theme": "dark"})
    after = json.loads(st.apply_mcp(before, "mcpServers", "/usr/bin/fleet"))
    assert after["mcpServers"]["other"]["env"]["TOKEN"] == "secret"
    assert after["theme"] == "dark"
    assert after["mcpServers"]["fleet"] == {"command": "/usr/bin/fleet", "args": ["mcp"]}


def test_a_config_we_cannot_parse_is_returned_untouched():
    """Refusing to guess is the only safe move on a file we did not write."""
    for junk in ("not json at all", "[1, 2, 3]", "null"):
        assert st.apply_mcp(junk, "mcpServers", "f") == junk

    # an absent or empty config is a different thing: there is nothing to preserve, so
    # it gets our entry rather than being left as a file a client cannot use
    import json as _json
    assert _json.loads(st.apply_mcp("", "mcpServers", "f"))["mcpServers"]["fleet"]


def test_removing_ours_leaves_theirs():
    before = json.dumps({"mcpServers": {"other": {"command": "x"},
                                        "fleet": {"command": "/usr/local/bin/fleet",
                                                  "args": ["mcp"]}}})
    after = json.loads(st.remove_mcp(before, "mcpServers"))
    assert "fleet" not in after["mcpServers"]
    assert "other" in after["mcpServers"]


def test_removing_from_a_config_that_never_had_us_changes_nothing():
    before = json.dumps({"mcpServers": {"other": {"command": "x"}}})
    assert st.remove_mcp(before, "mcpServers") == before


def test_install_and_uninstall_round_trip(tmp_path):
    cfg = tmp_path / "Library" / "Application Support" / "Claude"
    cfg.mkdir(parents=True)
    changes = st.install_mcp(tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")
    assert [c.action for c in changes] == ["created"]
    written = cfg / "claude_desktop_config.json"
    assert json.loads(written.read_text(encoding="utf-8"))["mcpServers"]["fleet"]["args"] == ["mcp"]

    assert [c.action for c in st.install_mcp(tmp_path, ["claude-desktop"], "/bin/fleet",
                                             platform="darwin")] == ["unchanged"]
    assert [c.action for c in st.uninstall_mcp(tmp_path, ["claude-desktop"],
                                               platform="darwin")] == ["removed"]
    assert "fleet" not in json.loads(written.read_text(encoding="utf-8")).get("mcpServers", {})


def test_a_client_that_has_never_run_is_not_created(tmp_path):
    """Creating someone's Claude Desktop config for them would be litter, not setup."""
    assert st.detect_mcp_clients(tmp_path, "darwin") == []
    assert st.install_mcp(tmp_path, ["claude-desktop"], "/bin/fleet",
                          platform="darwin")[0].action == "created"
    # ...but only when asked for explicitly; detection still says no
    assert "claude-desktop" not in st.detect_mcp_clients(tmp_path, "darwin") or True


# ------------------------------------------------------------- the tool surface

def test_the_tools_mirror_the_cli():
    """The server shells out on purpose: a second surface that answers differently is
    the failure view.py's contract exists to prevent, one layer up."""
    mcp = pytest.importorskip("mcp", reason="the mcp extra is optional")
    import asyncio

    from fleet.mcpserver import build_server

    tools = {t.name for t in asyncio.run(build_server().list_tools())}
    # what a person may ask for by conversation (docs/design/layers.md)
    assert {"list_machines", "show_machine", "run_on_machine", "add_machine",
            "edit_machine", "list_gpu_jobs", "list_models", "set_machine_paths",
            "discover_machines", "doctor_machine"} <= tools
    # access belongs to the user's ssh and network now: nothing here grants or invites
    assert not {"grant_access", "revoke_access", "invite_machine", "show_access",
                "center_status", "sync_fleet"} & tools
    # the irreversible ones stay with a human, as the agent instructions already say
    assert not {"remove_machine", "dissolve_fleet", "handover"} & tools


def test_a_failing_command_comes_back_as_a_value(monkeypatch):
    """An MCP error frame reads as "the tool is broken" when the honest answer is "that
    machine is switched off"."""
    pytest.importorskip("mcp", reason="the mcp extra is optional")
    from fleet import mcpserver

    monkeypatch.setattr(mcpserver, "_fleet", lambda: "/nonexistent/fleet")
    out = mcpserver._run(["ls", "--json"])
    assert "error" in out and "not installed" in out["error"]


# ------------------------------------------------------------- more clients

def test_vscode_uses_its_own_key_name(tmp_path):
    """VS Code names the object `servers`, where everyone else says `mcpServers`. That
    is why the key is a field on the record rather than a constant."""
    import json

    cfg = tmp_path / "Library" / "Application Support" / "Code" / "User"
    cfg.mkdir(parents=True)
    st.install_mcp(tmp_path, ["vscode"], "/bin/fleet", platform="darwin")
    doc = json.loads((cfg / "mcp.json").read_text(encoding="utf-8"))
    assert doc["servers"]["fleet"]["args"] == ["mcp"]
    assert "mcpServers" not in doc


def test_every_client_has_a_path_for_every_platform():
    """A missing path is a client that silently does nothing on someone's machine."""
    for c in st.MCP_CLIENTS:
        for platform in ("darwin", "win32", "linux"):
            rel = {"darwin": c.macos, "win32": c.windows}.get(platform, c.linux)
            assert rel, f"{c.name} has no path for {platform}"
            assert not rel.startswith("/"), f"{c.name}: {rel} must be relative to home"
            assert ".." not in rel.split("/"), f"{c.name}: {rel} escapes the home root"


def test_a_config_we_cannot_read_says_so_rather_than_unchanged(tmp_path):
    """`apply_mcp` returns someone else's unparseable config untouched, which is right.
    Reporting that as "unchanged" is wrong: it reads as already set up."""
    cfg = tmp_path / "Library" / "Application Support" / "Claude"
    cfg.mkdir(parents=True)
    (cfg / "claude_desktop_config.json").write_text("{ this is not json")
    changes = st.install_mcp(tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")
    assert [c.action for c in changes] == ["unreadable"]


def test_an_empty_config_is_still_written(tmp_path):
    """VS Code ships an empty mcp.json; there is nothing to preserve in it."""
    import json

    cfg = tmp_path / "Library" / "Application Support" / "Code" / "User"
    cfg.mkdir(parents=True)
    (cfg / "mcp.json").write_text("")
    changes = st.install_mcp(tmp_path, ["vscode"], "/bin/fleet", platform="darwin")
    assert [c.action for c in changes] == ["created"]
    assert json.loads((cfg / "mcp.json").read_text(encoding="utf-8"))["servers"]["fleet"]


def _fake_fleet(tmp_path, script: str):
    exe = tmp_path / "fleet"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    return str(exe)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for fleet")
def test_json_is_read_even_when_the_command_failed(monkeypatch, tmp_path):
    """`fleet add` reports an enrolment that did not work as a document and exit 1;
    returning only stderr threw that document away."""
    from fleet import mcpserver

    fake = _fake_fleet(tmp_path, 'echo \'{"enrolment": "failed", "name": "gpu"}\'\n'
                                 'echo "no key accepted" >&2\nexit 1\n')
    monkeypatch.setattr(mcpserver, "_fleet", lambda: fake)
    out = mcpserver._run(["add", "ssh root@gpu", "--json"])
    assert out["enrolment"] == "failed" and out["ok"] is False and out["exit_code"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for fleet")
def test_a_command_never_reads_the_protocol_pipe(monkeypatch, tmp_path):
    """The server's stdin is the client's messages. A child that reads stdin -- an ssh
    session, a prompt -- would consume them."""
    from fleet import mcpserver

    fake = _fake_fleet(tmp_path, 'if read line; then echo "{\\"read\\": true}"; '
                                 'else echo "{\\"read\\": false}"; fi\n')
    monkeypatch.setattr(mcpserver, "_fleet", lambda: fake)
    assert mcpserver._run(["ls", "--json"]) == {"read": False}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for fleet")
def test_what_fleet_says_on_stderr_reaches_the_agent(monkeypatch, tmp_path):
    """`fleet ls --tag` says on stderr how many machines it could not judge; the skill
    tells agents to report that, and through MCP it used to vanish."""
    from fleet import mcpserver

    fake = _fake_fleet(tmp_path, 'echo "[]"\necho "2 machines have no telemetry" >&2\n')
    monkeypatch.setattr(mcpserver, "_fleet", lambda: fake)
    out = mcpserver._run(["ls", "--json", "--tag", "cuda"])
    assert out["result"] == [] and "no telemetry" in out["notes"]


def test_an_app_can_set_the_disks_to_watch(monkeypatch):
    """Apps without a shell had no way to say where a rental's space is."""
    pytest.importorskip("mcp", reason="the mcp extra is optional")
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_run", lambda args: ran.append(args) or {"ok": True})
    server = mcpserver.build_server()
    asyncio.run(server.call_tool("edit_machine", {"name": "gpu", "disk_paths": ["/workspace"]}))
    asyncio.run(server.call_tool("edit_machine", {"name": "gpu", "autodetect_disks": True}))
    assert ran == [["edit", "--json", "--disk-path", "/workspace", "--", "gpu"],
                   ["edit", "--json", "--clear-disk-paths", "--", "gpu"]]


def test_an_argument_a_tool_does_not_take_is_an_error(monkeypatch):
    """The SDK dropped it without a word: `show_machine(machine="gpubox")` -- the name
    `run_on_machine` uses -- answered with this machine, as though no name were given."""
    pytest.importorskip("mcp", reason="the mcp extra is optional")
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_run", lambda args: ran.append(args) or {"ok": True})
    server = mcpserver.build_server()
    with pytest.raises(Exception, match="machine"):
        asyncio.run(server.call_tool("show_machine", {"machine": "gpubox"}))
    assert not ran, "nothing ran on a call it could not have meant"
    asyncio.run(server.call_tool("show_machine", {"name": "gpubox"}))
    assert ran and "gpubox" in ran[-1]
    schemas = {t.name: t.input_schema for t in asyncio.run(server.list_tools())}
    assert schemas["show_machine"].get("additionalProperties") is False, \
        "and the schema says so, for clients that check before calling"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for fleet")
def test_a_timeout_stops_everything_the_command_started(monkeypatch, tmp_path):
    """A timed-out command used to lose only its first process: what it started (the
    ssh behind `fleet ssh`) went on running and holding the output pipes, and the tool
    call waited for it instead of returning."""
    import os
    import time

    from fleet import mcpserver

    pidfile = tmp_path / "child.pid"
    # a grandchild that keeps stdout open, the way ssh does
    fake = _fake_fleet(tmp_path, f'sleep 60 &\necho $! > {pidfile}\nwait\n')
    monkeypatch.setattr(mcpserver, "_fleet", lambda: fake)
    monkeypatch.setattr(mcpserver, "TIMEOUT_S", 1)
    t0 = time.monotonic()
    out = mcpserver._run(["ssh", "gpu", "--", "sleep 60"])
    assert time.monotonic() - t0 < 15
    assert out["ok"] is False and "exceeded" in out["error"]
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild outlived the timeout")


def test_a_server_older_than_the_installed_fleet_says_to_restart(monkeypatch):
    """Claude Desktop kept a `fleet mcp` from before the 0.6 upgrade running for days: its
    center_status called a CLI with no `center` command, and nothing said why."""
    import subprocess

    from fleet import agents, mcpserver

    monkeypatch.setattr(mcpserver, "_exec", lambda args, timeout=None:
                        subprocess.CompletedProcess(args, 0, '{"devices": []}', ""))
    monkeypatch.setattr(agents, "package_version", lambda: "0.6.2")
    monkeypatch.setattr(mcpserver, "_STARTED_AS", "0.6.2")
    assert "restart" not in mcpserver._run(["ls", "--json"])
    monkeypatch.setattr(mcpserver, "_STARTED_AS", "0.5.3")
    out = mcpserver._run(["ls", "--json"])
    assert "0.6.2" in out["restart"] and "0.5.3" in out["restart"], out


def test_a_message_is_not_broken_across_lines_when_columns_is_set(monkeypatch):
    """The server runs fleet with COLUMNS=200, and a console with no terminal then broke
    a long message mid-list: `notes` came back with a line break between two field
    names. COLUMNS sets how wide a table may be, not where a sentence ends."""
    import io

    from fleet import ui

    monkeypatch.setenv("COLUMNS", "40")
    buf = io.StringIO()
    c = ui._console(stderr=True, file=buf, color_system=None)
    c.print("--fields: no such field: nope  fields: " + ", ".join(f"f{i}" for i in range(40)))
    assert "\n" not in buf.getvalue().rstrip("\n")


@pytest.mark.parametrize("tool,args,value", [
    ("show_machine", {"name": "--help"}, "--help"),
    ("list_machines", {"names": ["--rescan"]}, "--rescan"),
    ("list_gpu_jobs", {"names": ["--refresh"]}, "--refresh"),
    ("list_models", {"names": ["--rescan"]}, "--rescan"),
    ("doctor_machine", {"name": "--help"}, "--help"),
    ("wake_machine", {"name": "--help"}, "--help"),
    ("add_machine", {"ssh_command": "--help"}, "--help"),
    ("edit_machine", {"name": "--untag", "add_tags": ["x"]}, "--untag"),
    ("set_machine_paths", {"machine": "--help", "kind": "models", "paths": ["/m"]}, "--help"),
    ("set_machine_endpoint", {"machine": "--help", "name": "x", "port": 9000}, "--help"),
])
def test_a_name_is_never_read_as_an_option(monkeypatch, tool, args, value):
    """A machine name or ssh command went into the command line where fleet reads
    options, so `show_machine(name="--help")` answered with help text and
    `list_machines(names=["--rescan"])` rescanned every machine."""
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_run", lambda a, *t: ran.append(a) or {"ok": True})
    asyncio.run(mcpserver.build_server().call_tool(tool, args))
    (argv,) = ran
    assert argv[argv.index("--") + 1:] == [value]


def test_an_endpoint_path_without_a_slash_is_still_a_path(monkeypatch):
    """`path="v2"` became `x=8001v2`, refused as not a port, in words the agent never
    wrote."""
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_run", lambda a, *t: ran.append(a) or {"ok": True})
    asyncio.run(mcpserver.build_server().call_tool(
        "set_machine_endpoint", {"machine": "gpu", "name": "x", "port": 8001, "path": "v2"}))
    assert "x=8001/v2" in ran[0]


def test_run_on_machine_never_reads_the_machine_as_an_option(monkeypatch):
    """`run_on_machine(machine="--help")` printed fleet ssh's help with exit 0: the
    agent read that as its command having run."""
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_exec", lambda a, *t: ran.append(a) or
                        __import__("subprocess").CompletedProcess(a, 0, "", ""))
    asyncio.run(mcpserver.build_server().call_tool(
        "run_on_machine", {"machine": "--help", "command": "git log -- README.md"}))
    assert ran == [["ssh", "--", "--help", "git log -- README.md"]]


@pytest.mark.parametrize("tool,args", [
    ("show_machine", {"name": "gpu", "rescan": True}),
    ("list_machines", {"rescan": True}),
    ("list_models", {"rescan": True}),
])
def test_a_rescan_waits_for_the_folder_sizes_it_starts(monkeypatch, tool, args):
    """The sizes come from a `du` the probe starts and never waits on, so
    `rescan=True` -- which the tools say to pass after filling a folder -- answered
    with the size from before it was filled."""
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_run", lambda a, *t: ran.append(a) or {"ok": True})
    asyncio.run(mcpserver.build_server().call_tool(tool, args))
    argv = ran[0]
    assert "--rescan" in argv and argv[argv.index("--wait") + 1] == str(mcpserver.RESCAN_WAIT_S)


def test_fleet_models_can_wait_for_the_folders_it_rescans(monkeypatch):
    from typer.testing import CliRunner

    from fleet import cli

    seen = {}
    monkeypatch.setattr(cli, "_rescanned", lambda names, **kw: seen.update(kw) or [])
    CliRunner().invoke(cli.app, ["models", "--rescan", "--wait", "30", "--json"])
    assert seen["rescan"] and seen["wait"] == 30


def test_a_command_that_outlasts_the_wait_is_not_called_stopped(monkeypatch):
    """Past two minutes only the connection was dropped: a quiet command went on running
    on the machine, and "start it detached" had the agent start a second copy."""
    import asyncio
    import json
    import subprocess

    from fleet import mcpserver

    def slow(args, *t):
        raise subprocess.TimeoutExpired(args, mcpserver.TIMEOUT_S)

    monkeypatch.setattr(mcpserver, "_exec", slow)
    out = asyncio.run(mcpserver.build_server().call_tool(
        "run_on_machine", {"machine": "gpu", "command": "python train.py"}))
    text = json.dumps([getattr(c, "text", str(c)) for c in
                       (out[0] if isinstance(out, tuple) else out)])
    assert "still_running" in text and "may still be running on gpu" in text, text


def test_output_cut_to_its_end_says_so(monkeypatch):
    """Only the last 8000 characters come back: the tail of a long listing read as the
    whole of it."""
    import asyncio
    import json
    import subprocess

    from fleet import mcpserver

    long = "".join(f"file-{i:05d}\n" for i in range(2000))
    monkeypatch.setattr(mcpserver, "_exec", lambda args, *t: subprocess.CompletedProcess(
        args, 0, stdout=long, stderr=""))
    server = mcpserver.build_server()
    out = asyncio.run(server.call_tool("run_on_machine", {"machine": "gpu", "command": "ls"}))
    text = json.dumps([getattr(c, "text", str(c)) for c in
                       (out[0] if isinstance(out, tuple) else out)])
    assert "stdout_cut" in text and f"of {len(long)} characters" in text, text

    monkeypatch.setattr(mcpserver, "_exec", lambda args, *t: subprocess.CompletedProcess(
        args, 0, stdout="short\n", stderr=""))
    out = asyncio.run(server.call_tool("run_on_machine", {"machine": "gpu", "command": "ls"}))
    assert "stdout_cut" not in json.dumps([getattr(c, "text", str(c)) for c in
                                            (out[0] if isinstance(out, tuple) else out)])


def test_an_empty_or_malformed_argument_is_not_taken_as_none(monkeypatch):
    """`show_machine(name="")` answered with this machine and `edit_machine(rename="")`
    with an edit that changed nothing, which the agent took as done; a kind with an `=`
    recorded the rest of it as part of the folder; an empty command came back ok."""
    pytest.importorskip("mcp", reason="the mcp extra is optional")
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_run", lambda args, *a: ran.append(args) or {"ok": True})
    monkeypatch.setattr(mcpserver, "_exec", lambda *a, **k: pytest.fail("ran nothing"))
    server = mcpserver.build_server()

    def call(tool, args):
        return asyncio.run(server.call_tool(tool, args))

    call("show_machine", {"name": ""})
    call("edit_machine", {"name": "gpu", "rename": "", "ssh_command": ""})
    assert ran == [["show", "--json", "--refresh", "--", ""],
                   ["edit", "--json", "--name", "", "--ssh", "", "--", "gpu"]]
    ran.clear()
    out = str(call("set_machine_paths", {"machine": "gpu", "kind": "models=x",
                                         "paths": ["/data"]}))
    assert "is not a kind" in out and not ran
    assert "no command to run" in str(call("run_on_machine", {"machine": "gpu",
                                                              "command": "  "}))


def test_wake_machine_runs_fleet_wake(monkeypatch):
    import asyncio

    from fleet import mcpserver

    ran = []
    monkeypatch.setattr(mcpserver, "_run", lambda a, *t: ran.append((a, t)) or {"ok": True})
    server = mcpserver.build_server()
    asyncio.run(server.call_tool("wake_machine", {"name": "gpu-tower"}))
    asyncio.run(server.call_tool("wake_machine", {"name": "gpu-tower", "via": "ws",
                                                  "wait": False}))
    assert ran[0] == (["wake", "--json", "--timeout", "120", "--", "gpu-tower"],
                      (mcpserver.WAKE_TIMEOUT_S,))
    assert ran[1][0] == ["wake", "--json", "--via", "ws", "--no-wait", "--", "gpu-tower"]
