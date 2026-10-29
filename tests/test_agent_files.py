"""Setup writing into other programs' files, when those files are not as fleet expects.

Each test is a file a person really has -- saved by a Windows editor, kept in a dotfiles
repo, hand-edited, shared by several agents -- that setup used to crash on, damage, or
report wrongly.
"""

from __future__ import annotations

import json
import os

import pytest

from fleet.agents import clients, docs
from fleet.agents.docs import BEGIN, END


def _desktop(tmp_path):
    cfg = tmp_path / "Library" / "Application Support" / "Claude"
    cfg.mkdir(parents=True)
    return cfg / "claude_desktop_config.json"


def test_a_config_in_another_encoding_is_left_alone_not_a_crash(tmp_path):
    """A café in Latin-1 raised UnicodeDecodeError out of setup."""
    path = _desktop(tmp_path)
    raw = '{"mcpServers": {"x": {"command": "caf\xe9"}}}'.encode("latin-1")
    path.write_bytes(raw)
    assert [c.action for c in clients.install_mcp(
        tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")] == ["unreadable"]
    assert [c.action for c in clients.uninstall_mcp(
        tmp_path, ["claude-desktop"], platform="darwin")] == ["unreadable"]
    assert clients.installed_mcp_clients(tmp_path, "darwin") == []
    assert path.read_bytes() == raw


def test_the_first_run_finishes_whatever_an_agent_s_files_hold(monkeypatch):
    """It raised before writing its marker, so it ran -- and failed -- on every command:
    one odd config file and `fleet ls` never worked again."""
    from fleet.ops import firstrun

    monkeypatch.delenv("FLEET_NO_FIRST_RUN", raising=False)
    monkeypatch.setattr(firstrun, "add_this_machine", lambda: None)

    def broken():
        raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")

    monkeypatch.setattr(firstrun, "teach_agents", broken)
    said = []
    assert firstrun.maybe("ls", lambda line, level="ok": said.append((level, line))) is True
    assert said and said[0][0] == "warn" and "fleet setup" in said[0][1]
    assert firstrun.maybe("ls", lambda *a: None) is False          # done, not again


def test_a_byte_order_mark_and_crlf_are_kept(tmp_path):
    """Windows editors write both. The mark made the file read as unreadable; the line
    ends were rewritten, which shows as the whole file changed."""
    path = _desktop(tmp_path)
    path.write_bytes(b'\xef\xbb\xbf{\r\n  "mcpServers": {"x": {"command": "y"}}\r\n}\r\n')
    assert [c.action for c in clients.install_mcp(
        tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")] == ["updated"]
    out = path.read_bytes()
    assert out.startswith(b"\xef\xbb\xbf") and b"\r\n" in out and b"\n" not in out.replace(b"\r\n", b"")
    doc = json.loads(out.decode("utf-8-sig"))
    assert doc["mcpServers"]["fleet"]["command"] == "/bin/fleet" and "x" in doc["mcpServers"]
    assert clients.installed_mcp_clients(tmp_path, "darwin") == ["claude-desktop"]


def test_a_server_of_the_user_s_own_called_fleet_is_not_replaced(tmp_path):
    path = _desktop(tmp_path)
    theirs = {"command": "node", "args": ["/srv/fleet-tracker/index.js"]}
    path.write_text(json.dumps({"mcpServers": {"fleet": theirs}}))
    assert [c.action for c in clients.install_mcp(
        tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")] == ["taken"]
    clients.uninstall_mcp(tmp_path, ["claude-desktop"], platform="darwin")
    assert json.loads(path.read_text())["mcpServers"]["fleet"] == theirs


def test_our_older_entries_are_still_ours(tmp_path):
    """Before the rename the uvx entry named `agent-fleet`; a refresh must update it."""
    path = _desktop(tmp_path)
    path.write_text(json.dumps({"mcpServers": {"fleet": {
        "command": "/usr/bin/uvx", "args": ["agent-fleet", "mcp"]}}}))
    assert clients.installed_mcp_clients(tmp_path, "darwin") == ["claude-desktop"]
    assert [c.action for c in clients.install_mcp(
        tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")] == ["updated"]


def test_an_unparseable_config_that_mentions_fleet_is_not_unchanged(tmp_path):
    """Whether the text said "fleet" anywhere decided between "unreadable" and
    "unchanged", so a broken config naming a fleet path read as already set up."""
    path = _desktop(tmp_path)
    path.write_text('{"mcpServers": {"fleet": {"command": "/bin/fleet", // mine\n}}')
    assert [c.action for c in clients.install_mcp(
        tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")] == ["unreadable"]


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="symlinks need privileges on Windows")
def test_a_linked_config_is_written_through_the_link(tmp_path):
    """Kept in a dotfiles repo and linked in: the link was replaced by a plain file."""
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text("{}")
    path = _desktop(tmp_path)
    path.symlink_to(real)
    clients.install_mcp(tmp_path, ["claude-desktop"], "/bin/fleet", platform="darwin")
    assert path.is_symlink()
    assert json.loads(real.read_text())["mcpServers"]["fleet"]


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="symlinks need privileges on Windows")
def test_a_list_of_machines_shared_with_dotfiles_is_written_through_the_link(tmp_path):
    """The guides say to share machines.yaml with your dotfiles: the first edit renamed a
    plain file over the link, and the repo never saw that change or any after it."""
    from fleet.models import Device
    from fleet.state import inventory as inv

    real = tmp_path / "dotfiles" / "machines.yaml"
    real.parent.mkdir()
    inv.save([Device(id="id:a", name="a")], real)
    path = tmp_path / "config" / "machines.yaml"
    path.parent.mkdir()
    path.symlink_to(real)
    inv.update(lambda ds: (ds + [Device(id="id:b", name="b")], None), path)
    assert path.is_symlink()
    assert [d.name for d in inv.load(real)] == ["a", "b"]


# ------------------------------------------------------------------ marked regions
def test_a_begin_marker_without_its_end_loses_none_of_the_user_s_text():
    """The region ran from the stray BEGIN to the next END, taking what lay between."""
    text = f"intro\n{BEGIN}\nhalf-deleted\nMY NOTES\n"
    once = docs.apply_block(text, "ours")
    twice = docs.apply_block(once, "ours v2")
    assert "MY NOTES" in twice and "intro" in twice and "ours v2" in twice
    assert "MY NOTES" in docs.remove_block(twice)


def test_an_agents_md_in_another_encoding_is_left_alone(tmp_path):
    (tmp_path / ".codex").mkdir()
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(b"caf\xe9\n")
    changes = docs.install(tmp_path, ["codex"], "fleet", project=True)
    assert [c.action for c in changes] == ["unreadable"]
    assert agents.read_bytes() == b"caf\xe9\n"
    assert docs.installed_targets(tmp_path, project=True) == []
    assert [c.action for c in docs.uninstall(tmp_path, ["codex"], project=True)] == ["unreadable"]


def test_removing_a_region_that_was_the_whole_file_removes_the_file(tmp_path):
    docs.install(tmp_path, ["gemini"], "fleet", project=True)
    assert (tmp_path / "GEMINI.md").exists()
    docs.uninstall(tmp_path, ["gemini"], project=True)
    assert not (tmp_path / "GEMINI.md").exists()


# ------------------------------------------------------------------ shared skill
def test_uninstalling_one_agent_keeps_the_skill_others_read(tmp_path):
    """~/.agents/skills is read by Codex, Gemini, Copilot and more: taking fleet out of
    Codex took it from all of them."""
    for d in (".codex", ".gemini"):
        (tmp_path / d).mkdir()
    docs.install(tmp_path, ["codex", "gemini"], "fleet")
    skill = tmp_path / ".agents" / "skills" / "fleet" / "SKILL.md"
    assert skill.exists()
    [kept] = docs.uninstall(tmp_path, ["codex"])
    assert kept.action == "kept for gemini" and skill.exists()
    docs.uninstall(tmp_path, ["codex", "gemini"])
    assert not skill.exists()


def test_uninstall_takes_an_old_copy_out_when_the_current_one_is_gone(tmp_path):
    (tmp_path / ".codex").mkdir()
    old = tmp_path / ".codex" / "skills" / "fleet" / "SKILL.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\nname: fleet\n---\nold\n")
    assert "codex" in docs.installed_targets(tmp_path)
    changes = docs.uninstall(tmp_path, ["codex"])
    assert not old.exists()
    assert "removed" in [c.action for c in changes]


def test_a_copy_with_windows_line_ends_is_current(tmp_path):
    """Read as raw bytes, every copy Python wrote on Windows looked older than fleet's,
    so `fleet ls` always said to refresh and setup rewrote it each time."""
    (tmp_path / ".codex").mkdir()
    docs.install(tmp_path, ["codex"], "fleet", project=True)
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(agents.read_bytes().replace(b"\n", b"\r\n"))
    assert docs.stale_targets(tmp_path, "fleet", project=True) == []
    assert [c.action for c in docs.install(tmp_path, ["codex"], "fleet", project=True)] \
        == ["unchanged"]


def test_a_file_with_windows_line_ends_keeps_them(tmp_path):
    """Read with "\n" ends, an AGENTS.md with Windows ones went back with "\n": every one
    of the user's own lines changed by `fleet setup`, and again by `--uninstall`."""
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(b"# Mine\r\n\r\nUse tabs.\r\n")
    assert [c.action for c in docs.install(tmp_path, ["codex"], "fleet", project=True)] \
        == ["updated"]
    raw = agents.read_bytes()
    assert raw.startswith(b"# Mine\r\n\r\nUse tabs.\r\n") and b"BEGIN fleet" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    docs.uninstall(tmp_path, ["codex"], project=True)
    assert agents.read_bytes() == b"# Mine\r\n\r\nUse tabs.\r\n"
