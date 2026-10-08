"""`fleet uninstall`: out of the fleet, out of the agents, files gone only when asked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fleet import cli, config
from fleet.models import Device, Kind
from fleet.ops import member
from fleet.state import access as acl
from fleet.state import inventory as inv

A, B = "SHA256:aaa", "SHA256:bbb"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.setenv("HOME", str(h))
    return h


def _center(*, others: bool, monkeypatch):
    keys = {A: {"name": "hub", "pubkey": "ssh-ed25519 AAAA a", "device_id": "id:hub"}}
    if others:
        keys[B] = {"name": "box", "pubkey": "ssh-ed25519 AAAA b", "device_id": "id:box"}
    acl.save(acl.Access(fleet_id="f1", center=A, keys=keys))
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center")])
    monkeypatch.setattr(acl, "is_center", lambda *a, **k: True)


def test_a_center_with_machines_in_it_is_not_uninstalled(home, monkeypatch):
    """Its key is on every one of them, and only it can take it off."""
    _center(others=True, monkeypatch=monkeypatch)
    r = CliRunner().invoke(cli.app, ["uninstall", "--yes", "--purge"])
    assert r.exit_code == 2 and "--dissolve" in r.output
    assert acl.ACCESS_PATH.exists() and inv.INVENTORY_PATH.exists(), "nothing touched"


def test_an_empty_center_forgets_its_fleet_and_keeps_its_files_unless_asked(home, monkeypatch):
    _center(others=False, monkeypatch=monkeypatch)
    assert CliRunner().invoke(cli.app, ["setup", "--target", "claude"]).exit_code == 0
    skill = home / ".claude" / "skills" / "fleet" / "SKILL.md"
    assert skill.exists()

    r = CliRunner().invoke(cli.app, ["uninstall", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["left"] == "empty center" and not acl.ACCESS_PATH.exists()
    assert not skill.exists(), "the agents no longer hear about fleet"
    assert str(inv.INVENTORY_PATH) in out["files_kept"] and inv.INVENTORY_PATH.exists()
    assert out["remove_program"], "the last step is said"


def test_purge_deletes_fleets_files_and_nothing_else(home, monkeypatch):
    _center(others=False, monkeypatch=monkeypatch)
    config.FLEET_KEY.write_text("key")
    mine = config.CONFIG_DIR / "notes-of-someone-else.txt"
    mine.write_text("not fleet's")
    r = CliRunner().invoke(cli.app, ["uninstall", "--yes", "--purge", "--json"])
    assert r.exit_code == 0, r.output
    assert not inv.INVENTORY_PATH.exists() and not config.FLEET_KEY.exists()
    assert mine.exists(), "never by globbing a directory"


def test_a_member_leaves_first_and_is_told_to_remove_it_on_the_center(home, monkeypatch):
    acl.CACHE_PATH.write_text("center_pubkey: ssh-ed25519 AAAA c\n")
    left = []
    monkeypatch.setattr(member, "leave", lambda: left.append(1) or ("f1", 2))
    monkeypatch.setattr(member, "center_name", lambda: "hub")
    r = CliRunner().invoke(cli.app, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output
    assert left and "left fleet f1" in r.output and "fleet rm" in r.output


def test_without_a_terminal_it_asks_for_yes(home):
    r = CliRunner().invoke(cli.app, ["uninstall"])
    assert r.exit_code == 2 and "--yes" in r.output


def test_a_machine_in_no_fleet_is_uninstalled_too(home):
    r = CliRunner().invoke(cli.app, ["uninstall", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["left"] == ""
