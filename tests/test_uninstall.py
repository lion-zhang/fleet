"""`fleet uninstall`: out of the agents, files gone only when asked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fleet import cli, config
from fleet.models import Device, Kind
from fleet.state import inventory as inv


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.setenv("HOME", str(h))
    return h


def test_the_agents_no_longer_hear_about_fleet_and_its_files_stay_unless_asked(home):
    inv.save([Device(id="id:box", name="box", kind=Kind.PERMANENT)])
    assert CliRunner().invoke(cli.app, ["setup", "--target", "claude"]).exit_code == 0
    skill = home / ".claude" / "skills" / "fleet" / "SKILL.md"
    assert skill.exists()

    r = CliRunner().invoke(cli.app, ["uninstall", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert not skill.exists(), "the agents no longer hear about fleet"
    assert str(inv.INVENTORY_PATH) in out["files_kept"] and inv.INVENTORY_PATH.exists()
    assert out["remove_program"], "the last step is said"


def test_purge_deletes_fleets_files_and_nothing_else(home):
    inv.save([Device(id="id:box", name="box", kind=Kind.PERMANENT)])
    mine = config.CONFIG_DIR / "notes-of-someone-else.txt"
    mine.write_text("not fleet's")
    hidden = config.CONFIG_DIR / "discover-hidden.txt"       # `fleet discover --hide`
    hidden.write_text("nas\n")
    r = CliRunner().invoke(cli.app, ["uninstall", "--yes", "--purge", "--json"])
    assert r.exit_code == 0, r.output
    assert not inv.INVENTORY_PATH.exists()
    assert not hidden.exists(), "a file fleet wrote, left behind by --purge"
    assert mine.exists(), "never by globbing a directory"


def test_without_a_terminal_it_asks_for_yes(home):
    r = CliRunner().invoke(cli.app, ["uninstall"])
    assert r.exit_code == 2 and "--yes" in r.output


def test_a_client_config_that_holds_other_servers_says_only_fleet_left_it(home):
    """`✓ removed …/claude_desktop_config.json` read as though the file were gone, when
    only fleet's entry in it was."""
    import sys

    from fleet.agents.registry import MCP_CLIENTS

    client = next(c for c in MCP_CLIENTS if c.path(home, sys.platform))
    path = client.path(home, sys.platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({client.key: {"fleet": {"command": "fleet", "args": ["mcp"]},
                                             "other": {"command": "other"}}}))
    r = CliRunner().invoke(cli.app, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output
    assert "other" in json.loads(path.read_text())[client.key]
    flat = "".join(r.output.split())          # however the console wrapped the path
    assert "removedfleetfrom" + "".join(str(path).split()) in flat, r.output
