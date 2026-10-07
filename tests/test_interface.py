"""What agents and people see: found in the final audit by running every command."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from fleet import cli, reconcile as rec
from fleet.models import Device, Kind
from fleet.state import access as acl
from fleet.state import inventory as inv

A, B = "SHA256:aaa", "SHA256:bbb"


@pytest.fixture
def center(monkeypatch):
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    acl.save(acl.Access(fleet_id="f1", center=A, keys={
        A: {"name": "hub", "pubkey": "ssh-ed25519 AAAA a", "device_id": "id:hub"},
        B: {"name": "box", "pubkey": "ssh-ed25519 AAAA b", "device_id": "id:box"}}))
    monkeypatch.setattr(acl, "is_center", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_stepped_down", lambda acc: False)
    return CliRunner()


def test_a_name_must_be_one(center):
    r = center.invoke(cli.app, ["edit", "box", "--name", "", "--json"])
    assert r.exit_code != 0
    r = center.invoke(cli.app, ["edit", "box", "--name", "a b", "--json"])
    assert r.exit_code != 0 and "not a usable name" in r.output
    assert inv.find(inv.load(), "box") is not None
    assert inv.find(inv.load(), "") is None, "an empty name matches nothing"


def test_ls_of_a_name_that_matches_nothing_fails(center):
    r = center.invoke(cli.app, ["ls", "nosuch", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["unknown"] == ["nosuch"]


def test_sync_from_something_that_is_no_address(center):
    r = center.invoke(cli.app, ["sync", "--from", "http://"])
    assert r.exit_code == 2 and "Not an address" in r.output
    assert cli._center_url_from("10.0.0.5") == "http://10.0.0.5:7373/sync"
    assert cli._center_url_from("hub:8000") == "http://hub:8000/sync"


def test_an_invite_cannot_last_for_years(center):
    r = center.invoke(cli.app, ["invite", "--ttl", "99999d", "--json"])
    assert r.exit_code == 2 and "at most" in r.output


def test_the_center_cannot_leave_its_own_fleet(center):
    r = center.invoke(cli.app, ["center", "--leave"])
    assert r.exit_code == 2 and "--dissolve" in r.output


def test_access_states_are_the_words_the_docs_use(center):
    acc = acl.load()
    acl.grant(acc, A, B, user="root")
    acl.save(acc)
    rec.save_ledger({f"{A}>{B}>root": rec.EdgeState(desired="present", observed="unknown",
                                                     attempts=2, last_error="timed out")})
    rows = json.loads(center.invoke(cli.app, ["access", "--json"]).stdout)
    assert rows["is_center"] is True and rows["role"] == "center"
    edge = next(e for e in rows["edges"] if e["to"] == "box")
    assert edge["state"] == "pending" and edge["observed"] == "unknown"
    assert edge["attempts"] == 2


def test_top_needs_a_positive_interval(center):
    assert center.invoke(cli.app, ["top", "-i", "0"]).exit_code == 2


def test_a_member_told_to_ask_the_center_is_not_told_user_none(monkeypatch):
    acl.CACHE_PATH.write_text("center_pubkey: ssh-ed25519 AAAA c\n")
    inv.save([Device(id="id:gpu", name="gpu", kind=Kind.PERMANENT)])
    r = CliRunner().invoke(cli.app, ["access", "gpu", "--allow", "laptop"])
    assert "--user None" not in r.output
