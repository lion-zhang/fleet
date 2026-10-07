"""A member: in a fleet, not deciding. Found on a real fleet, where every command that
loaded the access list refused on a member with advice that would start a second fleet."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.models import Device, Kind
from fleet.state import access as acl
from fleet.state import inventory as inv

CENTER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEfO2BAfm3M8YGY39abf7HwQ9kNkKOR7nesP3z7/PatB fleet:hub"


@pytest.fixture
def member(tmp_path, monkeypatch):
    acl.pin_center_pubkey(CENTER)
    acl.note_center_url("http://hub:7373/sync")
    acl.note_center_seen()
    acl.note_fleet_id("4b6d36")
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center",
                     pubkey=CENTER),
              Device(id="id:me", name="worker", kind=Kind.PERMANENT)])
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_center_json_answers_is_center_false(member):
    r = CliRunner().invoke(cli.app, ["center", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["is_center"] is False and out["member"] is True
    assert out["role"] == "member", "the install mode, in one word"
    assert out["center"] == "hub" and out["fleet_id"] == "4b6d36"


def test_center_on_a_member_says_who_decides(member):
    r = CliRunner().invoke(cli.app, ["center"])
    assert r.exit_code == 0, r.output
    assert "hub" in r.output and "--init" not in r.output


def test_outside_any_fleet_it_names_both_ways_in():
    r = CliRunner().invoke(cli.app, ["center"])
    assert r.exit_code == 2
    flat = " ".join(r.output.split())
    assert "fleet center --init" in flat and "fleet invite" in flat
    r = CliRunner().invoke(cli.app, ["center", "--json"])
    assert r.exit_code == 0 and json.loads(r.output) == {"role": "", "is_center": False,
                                                         "member": False}


def test_access_on_a_member_points_at_the_center(member):
    r = CliRunner().invoke(cli.app, ["access"])
    assert r.exit_code == 0, r.output
    assert "hub" in r.output


def test_a_member_cannot_grant_and_is_told_where_to(member):
    """It used to "file a request" into an outbox nothing ever read."""
    r = CliRunner().invoke(cli.app, ["access", "gpu", "--allow", "worker"])
    assert r.exit_code == 2
    assert "fleet access gpu --allow worker" in r.output and "hub" in r.output
    assert not acl.OUTBOX_PATH.exists()


def test_leaving_removes_this_fleets_blocks_and_nothing_else(member):
    keys = member / ".ssh" / "authorized_keys"
    keys.write_text(
        "ssh-ed25519 AAAA mine\n"
        "# fleet:4b6d36:begin from=SHA256:center user=root\nssh-ed25519 AAAA c\n"
        "# fleet:4b6d36:end from=SHA256:center\n"
        "# fleet:4b6d36:begin from=SHA256:peer user=root\nssh-ed25519 AAAA p\n"
        "# fleet:4b6d36:end from=SHA256:peer\n"
        "# fleet:otherf:begin from=SHA256:x user=root\nssh-ed25519 AAAA other\n"
        "# fleet:otherf:end from=SHA256:x\n")
    r = CliRunner().invoke(cli.app, ["center", "--leave"])
    assert r.exit_code == 0, r.output
    left = keys.read_text(encoding="utf-8")
    assert "AAAA mine" in left and "AAAA other" in left
    assert "AAAA c" not in left and "AAAA p" not in left
    assert acl.trusted_center_pubkey() == ""
    assert "removed 2 key block" in r.output


def test_sync_on_a_member_asks_the_listener(member, monkeypatch):
    from fleet.ops import sync

    asked = []
    monkeypatch.setattr(sync, "ensure_fresh", lambda force=False: asked.append(force))
    r = CliRunner().invoke(cli.app, ["sync"])
    assert asked == [True]
    assert "did not answer" in r.output and r.exit_code == 1


def test_a_member_cannot_remove_another_machine(member):
    r = CliRunner().invoke(cli.app, ["rm", "hub", "-y"])
    assert r.exit_code == 2 and "Only the center" in r.output
    assert "hub" in {d.name for d in inv.live(inv.load())}
