"""Agents learn what fleet does from a skill file, and an update used to leave it stale.

`fleet update` replaced the program and nothing else, so every agent kept describing the
commands as they were -- including ones that had since changed behaviour. Now the
install script refreshes what is already installed, and only that.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from fleet import cli
from fleet.agents import (installed_targets, skill_text, stale_targets, stamp)
from fleet.agents.registry import MCP_CLIENTS


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli, "fleet_command", lambda: "fleet")
    return home


def test_the_skill_carries_a_stamp_that_moves_with_its_words():
    """The version stayed at 0.4.0 through a run of changes to what agents are told."""
    assert stamp("fleet") in skill_text("fleet")
    assert stamp("fleet") != stamp("/opt/fleet/bin/fleet")


def test_refresh_rewrites_a_stale_skill(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    skill = home / ".claude" / "skills" / "fleet" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: fleet\n---\nan old description\n")
    assert stale_targets(home, "fleet") == ["claude"]
    r = CliRunner().invoke(cli.app, ["setup", "--refresh"])
    assert r.exit_code == 0, r.output
    assert skill.read_text() == skill_text("fleet")
    assert stale_targets(home, "fleet") == []


def test_refresh_never_adds_fleet_to_an_agent(tmp_path, monkeypatch):
    """It runs on every update; an agent somebody left alone must stay alone."""
    home = _home(tmp_path, monkeypatch)
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    r = CliRunner().invoke(cli.app, ["setup", "--refresh"])
    assert r.exit_code == 0, r.output
    assert "nothing to refresh" in r.output
    assert not (home / ".claude" / "skills").exists()
    assert installed_targets(home) == []


def test_refresh_touches_only_our_region_in_a_shared_file(tmp_path, monkeypatch):
    from fleet.agents import BEGIN, END

    home = _home(tmp_path, monkeypatch)
    agents_md = home / ".gemini" / "GEMINI.md"
    agents_md.parent.mkdir(parents=True)
    agents_md.write_text(f"my own notes\n\n{BEGIN}\nSTALE-FLEET-TEXT\n{END}\n\nmore of mine\n")
    CliRunner().invoke(cli.app, ["setup", "--refresh"])
    text = agents_md.read_text()
    assert text.startswith("my own notes") and text.rstrip().endswith("more of mine")
    assert "STALE-FLEET-TEXT" not in text and stamp("fleet") in text


def test_refresh_updates_an_mcp_entry_only_where_fleet_is_registered(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "config_command", lambda: "/new/fleet")
    client = MCP_CLIENTS[0]
    import sys
    path = client.path(home, sys.platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({client.key: {"fleet": {"command": "/old/fleet",
                                                       "args": ["mcp"]},
                                             "other": {"command": "x"}}}))
    CliRunner().invoke(cli.app, ["setup", "--refresh"])
    doc = json.loads(path.read_text())
    assert doc[client.key]["fleet"]["command"] == "/new/fleet"
    assert doc[client.key]["other"] == {"command": "x"}


def test_ls_says_when_the_agents_copy_is_older(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    skill = home / ".claude" / "skills" / "fleet" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old\n")
    assert "fleet setup --refresh" in cli._skills_note()
    skill.write_text(skill_text("fleet"))
    assert cli._skills_note() == ""


def test_every_install_and_update_refreshes_the_skills():
    from fleet.install import install_script

    for platform in ("posix", "windows"):
        for update_only in (False, True):
            script = install_script("git://hub/fleet.git", platform=platform,
                                    update_only=update_only)
            assert "setup --refresh" in script, (platform, update_only)
            # and before the version line, which is what the caller reports
            assert script.index("setup --refresh") < script.index("--version")
