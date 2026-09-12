"""The center's pass: making authorized_keys match the list.

Before this existed, `fleet access --allow` wrote a grant that nothing ever applied --
the list recorded intent perfectly and no key ever moved.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fleet import access as acl
from fleet import cli
from fleet import inventory as inv
from fleet import reconcile as rec
from fleet import store
from fleet.models import Device, Kind

A = "SHA256:aaa"
B = "SHA256:bbb"
C = "SHA256:ccc"


@pytest.fixture
def fleet_at(tmp_path, monkeypatch):
    for name in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, name, tmp_path / getattr(acl, name).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cli, "local_device_id", lambda: "id:center")

    devices = [
        Device(id="id:center", name="macbook", kind=Kind.PERMANENT, role="center"),
        Device(id="id:oracle", name="oracle", kind=Kind.PERMANENT,
               endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}]),
        Device(id="id:xps", name="lin-xps", kind=Kind.PERMANENT,
               endpoints=[{"target": "5.6.7.8", "user": "lin", "port": 22}]),
    ]
    inv.save(devices, inv.INVENTORY_PATH)
    acc = acl.Access(fleet_id="7f3a9c", center=A, keys={
        A: {"name": "macbook", "pubkey": "ssh-ed25519 AAAA a", "device_id": "id:center"},
        B: {"name": "oracle", "pubkey": "ssh-ed25519 AAAA b", "device_id": "id:oracle"},
        C: {"name": "lin-xps", "pubkey": "ssh-ed25519 AAAA c", "device_id": "id:xps"},
    })
    acl.save(acc, acl.ACCESS_PATH)
    return CliRunner(), tmp_path


def test_a_grant_is_actually_applied(fleet_at, monkeypatch):
    """The gap this closes: the list said `present`, and no key ever moved."""
    runner, _ = fleet_at
    calls = []
    monkeypatch.setattr(rec, "apply_edge",
                        lambda acc, edge, ep, **kw: calls.append((edge, kw)) or (True, ""))
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert calls, "the sweep never reached the reconciler"
    # set iteration order is not a contract; assert on the set of edges
    assert {e for e, _ in calls} == {(A, B, "root"), (A, C, "root")}
    assert all(kw["install"] is True for _, kw in calls)
    assert "installed on oracle" in r.output


def test_the_ledger_remembers_what_landed(fleet_at, monkeypatch):
    runner, _ = fleet_at
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    runner.invoke(cli.app, ["sync"])
    ledger = rec.load_ledger(acl.LEDGER_PATH)
    assert ledger[f"{A}>{B}>root"].observed == "present"

    # and a second pass has nothing to do
    r = runner.invoke(cli.app, ["sync"])
    assert "up to date" in r.output


def test_an_unreachable_device_stays_pending_and_says_so(fleet_at, monkeypatch):
    """A device that is off is not an error, it is an edge that has not converged --
    and reporting it as done would be the exact lie this design exists to remove."""
    runner, _ = fleet_at
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (False, "timed out"))

    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, "one dead host does not fail the sweep"
    assert "not reached" in r.output
    assert "still pending" in r.output
    st = rec.load_ledger(acl.LEDGER_PATH)[f"{A}>{B}>root"]
    assert st.observed != "present" and st.attempts == 1 and st.last_error


def test_a_revoke_is_applied_as_a_removal(fleet_at, monkeypatch):
    """One edge dropped while others remain -- the ordinary revoke. Dropping the *last*
    one is a different shape and is refused; see the test below."""
    runner, _ = fleet_at
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    acc = acl.load(acl.ACCESS_PATH)
    acl.grant(acc, C, B, user="root")     # something extra to survive the revoke
    acl.save(acc, acl.ACCESS_PATH)
    runner.invoke(cli.app, ["sync"])

    acc = acl.load(acl.ACCESS_PATH)
    acl.revoke(acc, C, B, user="root")
    acl.save(acc, acl.ACCESS_PATH)

    seen = []
    monkeypatch.setattr(rec, "apply_edge",
                        lambda acc_, edge, ep, **kw: seen.append(kw["install"]) or (True, ""))
    r = runner.invoke(cli.app, ["sync"])
    assert seen == [False], "the dropped edge becomes a removal, not silence"
    assert "removed from" in r.output


def test_a_revoke_still_works_after_the_machine_left_the_list(fleet_at, monkeypatch):
    """The ledger records which device an edge points at, because a revoke usually runs
    *because* the machine was dropped -- and without that the key would stay installed
    forever with nothing left to say where it was."""
    runner, _ = fleet_at
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    acc = acl.load(acl.ACCESS_PATH)
    acl.grant(acc, C, B, user="root")
    acl.save(acc, acl.ACCESS_PATH)
    runner.invoke(cli.app, ["sync"])

    assert rec.load_ledger(acl.LEDGER_PATH)[f"{A}>{B}>root"].dst_device == "id:oracle"


def test_the_sweep_refuses_to_strip_everything_at_once(fleet_at, monkeypatch):
    runner, _ = fleet_at
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    runner.invoke(cli.app, ["sync"])

    acl.save(acl.Access(fleet_id="7f3a9c"), acl.ACCESS_PATH)   # an empty list
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 2
    assert "refusing to remove everything" in r.output


def test_a_machine_that_is_not_the_center_does_not_sweep(fleet_at, monkeypatch):
    runner, _ = fleet_at
    monkeypatch.setattr(cli, "local_device_id", lambda: "id:oracle")
    monkeypatch.setattr(rec, "apply_edge",
                        lambda *a, **k: pytest.fail("a spoke must never reconcile"))
    runner.invoke(cli.app, ["sync"])


# --------------------------------------------------------------- removing a machine

def test_a_spoke_cannot_remove_another_machine(fleet_at, monkeypatch):
    """Removing a machine revokes its keys everywhere, which only the center can do."""
    runner, _ = fleet_at
    monkeypatch.setattr(cli, "local_device_id", lambda: "id:oracle")
    r = runner.invoke(cli.app, ["rm", "lin-xps", "-y"])
    assert r.exit_code == 2
    assert "Only the center" in r.output
    assert inv.find(inv.load(inv.INVENTORY_PATH), "lin-xps") is not None


def test_a_machine_can_always_remove_itself(fleet_at, monkeypatch):
    """That is leaving, and it needs nobody's permission: you own the machine you are
    standing on."""
    runner, _ = fleet_at
    monkeypatch.setattr(cli, "local_device_id", lambda: "id:oracle")
    r = runner.invoke(cli.app, ["rm", "oracle", "-y"])
    assert r.exit_code == 0, r.output
    assert inv.find(inv.load(inv.INVENTORY_PATH), "oracle") is None


def test_the_center_removing_a_machine_drops_its_edges(fleet_at, monkeypatch):
    """`fleet rm` used to leave every key installed forever, with merge never syncing
    the deletion either."""
    runner, _ = fleet_at
    monkeypatch.setattr(acl, "is_center", lambda *a, **k: True)
    r = runner.invoke(cli.app, ["rm", "oracle", "-y"])
    assert r.exit_code == 0, r.output
    acc = acl.load(acl.ACCESS_PATH)
    assert B not in acc.keys
    assert not any(B in (e.src, e.dst) for e in acc.allow)
    assert "still installed" in r.output, "and it says the keys have not gone yet"
