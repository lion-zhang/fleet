"""Creating a fleet and taking it down again.

`--init` existed from the start; nothing undid it. Deleting access.yaml by hand is not
dissolution -- it is orphaning: `access.load` then raises, so the center can no longer
manage the fleet, and every machine keeps its keys with nothing able to reach them.
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from fleet.state import access as acl
from fleet import cli
from fleet.state import inventory as inv
from fleet import reconcile as rec
from fleet.state import store
from fleet.models import Device, Kind, ProbeResult, Status


@pytest.fixture
def a_fleet(tmp_path, monkeypatch):
    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")

    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    pub = key.with_suffix(".pub").read_text()
    import fleet.config
    import fleet.ssh.keys
    monkeypatch.setattr(fleet.config, "FLEET_KEY", key)
    monkeypatch.setattr(fleet.ssh.keys, "ensure_keypair", lambda *a, **k: (key, pub))
    monkeypatch.setattr(cli, "ensure_keypair", lambda *a, **k: (key, pub), raising=False)

    me = acl.fingerprint(pub)
    inv.save([Device(id="id:me", name="macbook", kind=Kind.PERMANENT),
              Device(id="id:xps", name="lin-xps", kind=Kind.PERMANENT,
                     endpoints=[{"target": "5.6.7.8", "user": "lin", "port": 22}])],
             inv.INVENTORY_PATH)
    acl.save(acl.Access(fleet_id="7f3a9c", center=me, keys={
        me: {"name": "macbook", "pubkey": pub, "device_id": "id:me"},
        "SHA256:xps": {"name": "lin-xps", "pubkey": "ssh-ed25519 AAAA x",
                       "device_id": "id:xps"},
    }), acl.ACCESS_PATH)
    return CliRunner(), me


def test_dissolving_removes_the_keys_then_forgets_the_fleet(a_fleet, monkeypatch):
    """Order is not negotiable: the list is the only record of where the keys went, so
    forgetting it first would strand them."""
    runner, _ = a_fleet
    removed = []
    monkeypatch.setattr(rec, "apply_edge",
                        lambda acc, edge, ep, **kw: removed.append(kw["install"]) or (True, ""))

    r = runner.invoke(cli.app, ["center", "--dissolve"], input="y\n")
    assert r.exit_code == 0, r.output
    assert removed and all(i is False for i in removed), "every edge removed, none added"
    assert not acl.ACCESS_PATH.exists(), "and only then is the fleet forgotten"


def test_a_machine_that_cannot_be_reached_keeps_the_fleet_alive(a_fleet, monkeypatch):
    """Forgetting the fleet while a machine still holds keys leaves them installed with
    nothing able to remove them, so by default it stops and says so."""
    runner, _ = a_fleet
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (False, "no route"))

    r = runner.invoke(cli.app, ["center", "--dissolve"], input="y\n")
    assert r.exit_code == 1
    assert "still hold keys" in r.output
    assert acl.ACCESS_PATH.exists(), "kept, so you can finish the job"


def test_force_dissolves_anyway_and_names_what_was_stranded(a_fleet, monkeypatch):
    runner, _ = a_fleet
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (False, "no route"))

    r = runner.invoke(cli.app, ["center", "--dissolve", "--force"])
    assert r.exit_code == 0, r.output
    assert not acl.ACCESS_PATH.exists()
    assert "kept their keys" in r.output and "lin-xps" in r.output


def test_the_centers_own_key_comes_off_each_machine_last(a_fleet, monkeypatch):
    """Every removal logs in with the center's key. Taking it off a machine first made
    every other block there refused -- a clean machine reported as still holding keys
    (found on a real five-machine fleet)."""
    runner, me = a_fleet
    acc = acl.load(acl.ACCESS_PATH)
    acc.keys["SHA256:other"] = {"name": "worker", "pubkey": "ssh-ed25519 AAAA w",
                                "device_id": "id:w"}
    inv.save(inv.load(inv.INVENTORY_PATH) + [Device(
        id="id:w", name="worker", kind=Kind.PERMANENT,
        endpoints=[{"target": "192.0.2.9", "user": "root", "port": 22}])], inv.INVENTORY_PATH)
    acl.grant(acc, "SHA256:other", "SHA256:xps", user="lin")
    acl.save(acc, acl.ACCESS_PATH)
    locked_out: set[str] = set()

    def apply_edge(acc, edge, ep, **kw):
        src, dst, _ = edge
        if dst in locked_out:
            return False, "Permission denied (publickey)"
        if src == me:
            locked_out.add(dst)
        return True, ""
    monkeypatch.setattr(rec, "apply_edge", apply_edge)

    r = runner.invoke(cli.app, ["center", "--dissolve"], input="y\n")
    assert r.exit_code == 0, r.output
    assert "still hold keys" not in r.output
    assert r.output.count("keys removed from lin-xps") == 1, "one line per machine"


def test_a_machine_already_removed_is_not_reported_as_holding_keys(a_fleet, monkeypatch):
    """`fleet rm` took its keys off and dropped it from the list; its old edges in the
    ledger were listed by bare fingerprint as if still held."""
    runner, me = a_fleet
    ledger = rec.load_ledger()
    ledger[f"{me}>SHA256:gone>root"] = rec.EdgeState(desired="present", observed="absent")
    rec.save_ledger(ledger)
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))

    r = runner.invoke(cli.app, ["center", "--dissolve", "--force"])
    assert r.exit_code == 0, r.output
    assert "SHA256:gone" not in r.output and "kept their keys" not in r.output


def test_declining_the_prompt_changes_nothing(a_fleet, monkeypatch):
    runner, _ = a_fleet
    monkeypatch.setattr(rec, "apply_edge",
                        lambda *a, **k: pytest.fail("nothing should be touched"))
    r = runner.invoke(cli.app, ["center", "--dissolve"], input="n\n")
    assert r.exit_code == 1
    assert acl.ACCESS_PATH.exists()


def test_only_the_center_can_dissolve(a_fleet, monkeypatch):
    runner, _ = a_fleet
    monkeypatch.setattr(acl, "is_center", lambda *a, **k: False)
    r = runner.invoke(cli.app, ["center", "--dissolve"], input="y\n")
    assert r.exit_code == 2
    assert "--leave" in r.output, "and it points at the thing a spoke can do"


def test_the_wipe_guard_yields_only_when_dissolving():
    """"Remove everything" is always a bug except this once, and the guard cannot tell
    the difference on its own -- so dissolution says so explicitly."""
    empty = acl.Access(fleet_id="7f3a9c")
    installed = {"a>b>root": rec.EdgeState(observed="present")}
    assert rec.refuses_to_run(empty, installed)
    assert not rec.refuses_to_run(empty, installed, dissolving=True)


def test_a_fleet_can_be_created_again_afterwards(a_fleet, monkeypatch):
    runner, _ = a_fleet
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    runner.invoke(cli.app, ["center", "--dissolve"], input="y\n")
    monkeypatch.setattr(__import__("fleet.onboard").onboard, "onboard_self",
                        lambda **k: (Device(id="id:me", name="macbook",
                                            kind=Kind.PERMANENT),
                                     ProbeResult(status=Status.OK)))
    r = runner.invoke(cli.app, ["center", "--init"])
    assert r.exit_code == 0, r.output
    assert acl.load(acl.ACCESS_PATH).fleet_id != "7f3a9c", "a new fleet, not the old one"


def test_creating_a_fleet_puts_the_center_in_its_own_inventory(a_fleet, monkeypatch):
    """Seeded from the same object the access list was pinned from, or the two derive a
    name each and nothing reconciles them -- the access list answering to one name while
    `fleet show` knows the other. It also spares the user a `fleet add --self` they have
    no way to know they need."""
    runner, _ = a_fleet
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    runner.invoke(cli.app, ["center", "--dissolve"], input="y\n")
    monkeypatch.setattr(__import__("fleet.onboard").onboard, "onboard_self",
                        lambda **k: (Device(id="id:me", name="macbook",
                                            kind=Kind.PERMANENT),
                                     ProbeResult(status=Status.OK)))
    assert runner.invoke(cli.app, ["center", "--init"]).exit_code == 0

    assert "macbook" in {d.name for d in inv.load(inv.INVENTORY_PATH)}
    acc = acl.load(acl.ACCESS_PATH)
    assert acc.name_of(acc.center) == "macbook", "one name, not two"


def test_fleet_directories_are_owner_only_whatever_the_umask(tmp_path, monkeypatch):
    """Found on a real center run under `docker exec`, whose umask is 0000: the access
    list came out world-writable, which is every machine to any local user."""
    import os
    import sys

    import pytest

    if sys.platform == "win32":
        pytest.skip("POSIX modes")
    from fleet import config

    cfg, state = tmp_path / "cfg" / "fleet", tmp_path / "state" / "fleet"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "STATE_DIR", state)
    old = os.umask(0)
    try:
        config.ensure_dirs()
    finally:
        os.umask(old)
    for d in (cfg, state):
        assert d.stat().st_mode & 0o777 == 0o700, oct(d.stat().st_mode)


def test_an_existing_open_directory_is_tightened(tmp_path, monkeypatch):
    import sys

    import pytest

    if sys.platform == "win32":
        pytest.skip("POSIX modes")
    from fleet import config

    d = tmp_path / "fleet"
    d.mkdir(mode=0o777)
    d.chmod(0o777)
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(config, "STATE_DIR", d)
    config.ensure_dirs()
    assert d.stat().st_mode & 0o777 == 0o700
