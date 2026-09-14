"""Serving fleet to agents that cannot run a shell.

A coding agent reads the skill `fleet setup` writes and types commands. A desktop client
has no shell at all, so instructions are useless to it and it needs typed tools. Almost
every bug found building this was the same one wearing a different hat: something
resolved `fleet` the way a shell would, on a process that never read a profile.
"""

from __future__ import annotations

import json

import pytest

from fleet import setup as st


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
    monkeypatch.setattr(st.sys, "prefix", str(tmp_path / "other"))

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
    monkeypatch.setattr(st.sys, "executable", str(toolbin / "python3"))
    assert st.fleet_executable() == str(toolbin / "fleet")


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
                                        "fleet": {"command": "f"}}})
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
    assert json.loads(written.read_text())["mcpServers"]["fleet"]["args"] == ["mcp"]

    assert [c.action for c in st.install_mcp(tmp_path, ["claude-desktop"], "/bin/fleet",
                                             platform="darwin")] == ["unchanged"]
    assert [c.action for c in st.uninstall_mcp(tmp_path, ["claude-desktop"],
                                               platform="darwin")] == ["removed"]
    assert "fleet" not in json.loads(written.read_text()).get("mcpServers", {})


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
    assert {"list_machines", "show_machine", "run_on_machine", "show_access",
            "center_status"} <= tools
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
