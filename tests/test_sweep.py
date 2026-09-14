"""The center's pass: making authorized_keys match the list.

Before this existed, `fleet access --allow` wrote a grant that nothing ever applied --
the list recorded intent perfectly and no key ever moved.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fleet.state import access as acl
from fleet import cli
from fleet.ops import identity
from fleet.state import inventory as inv
from fleet import reconcile as rec
from fleet.state import store
from fleet.models import Device, Kind
from fleet.ops import enrol
from fleet.ops import sweep
from fleet.ops import sync

A = "SHA256:aaa"
B = "SHA256:bbb"
C = "SHA256:ccc"


@pytest.fixture
def fleet_at(tmp_path, monkeypatch):
    for name in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, name, tmp_path / getattr(acl, name).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:center")
    # the sweep hands the inventory to every machine at the end; that is a real
    # ssh per device, and these tests are about the reconciler
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: (255, ""))
    # broadcast seals once then sends per machine, so the stub goes
    # on the half that dials; sealing would shell out to ssh-keygen.
    monkeypatch.setattr(sync, "sealed_envelope", lambda payload: payload)
    monkeypatch.setattr(sync, "send_sealed", lambda *a, **k: (255, ""))

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
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

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
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
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
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

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
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    acc = acl.load(acl.ACCESS_PATH)
    acl.grant(acc, C, B, user="root")
    acl.save(acc, acl.ACCESS_PATH)
    runner.invoke(cli.app, ["sync"])

    assert rec.load_ledger(acl.LEDGER_PATH)[f"{A}>{B}>root"].dst_device == "id:oracle"


def test_the_sweep_refuses_to_strip_everything_at_once(fleet_at, monkeypatch):
    runner, _ = fleet_at
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    runner.invoke(cli.app, ["sync"])

    acl.save(acl.Access(fleet_id="7f3a9c"), acl.ACCESS_PATH)   # an empty list
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 2
    assert "refusing to remove everything" in r.output


def test_a_machine_that_is_not_the_center_does_not_sweep(fleet_at, monkeypatch):
    runner, _ = fleet_at
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:oracle")
    monkeypatch.setattr(rec, "apply_edge",
                        lambda *a, **k: pytest.fail("a spoke must never reconcile"))
    runner.invoke(cli.app, ["sync"])


# --------------------------------------------------------------- removing a machine

def test_a_spoke_cannot_remove_another_machine(fleet_at, monkeypatch):
    """Removing a machine revokes its keys everywhere, which only the center can do."""
    runner, _ = fleet_at
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:oracle")
    r = runner.invoke(cli.app, ["rm", "lin-xps", "-y"])
    assert r.exit_code == 2
    assert "Only the center" in r.output
    assert inv.find(inv.load(inv.INVENTORY_PATH), "lin-xps") is not None


def test_a_machine_can_always_remove_itself(fleet_at, monkeypatch):
    """That is leaving, and it needs nobody's permission: you own the machine you are
    standing on."""
    runner, _ = fleet_at
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:oracle")
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


# ------------------------------------------------------------- the trimmed surface

def test_ls_can_name_devices_so_refresh_is_not_needed(fleet_at):
    """`fleet refresh` was `_rows(refresh=True)` followed by a worse printer, and `ls`
    had no way to filter -- so the one thing refresh could do that ls could not was the
    reason to keep it."""
    runner, _ = fleet_at
    r = runner.invoke(cli.app, ["ls", "oracle", "--json"])
    assert r.exit_code == 0, r.output
    assert '"oracle"' in r.output
    assert '"lin-xps"' not in r.output


def test_refresh_is_gone():
    runner = CliRunner()
    assert runner.invoke(cli.app, ["refresh"]).exit_code != 0


def test_probe_still_works_but_is_not_advertised():
    """Its real job is capturing parser fixtures; without --raw it says what `fleet show
    --json` already says."""
    out = CliRunner().invoke(cli.app, ["--help"]).output
    assert "probe" not in out
    assert "Usage" in CliRunner().invoke(cli.app, ["probe", "--help"]).output


def test_paths_names_every_file_and_the_shared_directory(fleet_at):
    """CONFIG_DIR and STATE_DIR are the same directory on macOS, which is why every
    filename is distinct and nothing here may be cleaned up by globbing."""
    runner, _ = fleet_at
    out = runner.invoke(cli.app, ["paths"]).output
    for expected in ("inventory", "fleet key", "access", "ledger", "outbox", "cache"):
        assert expected in out
    assert "same directory" in out


# ------------------------------------------------------ one machine at a time

def test_machines_are_swept_at_once_but_one_machine_is_swept_in_order(fleet_at,
                                                                     monkeypatch):
    """A sweep opened up to four SSH handshakes per machine and did it serially, so the
    slowest box set the pace for the whole fleet. Fanning out across machines is safe;
    fanning out *within* one is not -- two edges on the same host edit the same
    authorized_keys, and interleaving them is how a marker block gets written twice or
    lost. reconcile passes multiplex=False for the same reason.

    Both halves are asserted, because a fan-out that quietly stopped fanning out would
    pass a test that only checked the serial half.
    """
    import threading
    import time as _time

    runner, _ = fleet_at
    # The center's edges to oracle and lin-xps come for free. Add lin-xps -> oracle so
    # that oracle has two edges of its own: without a host that is named twice, the
    # serial half of this test would have nothing to catch.
    acc = acl.load(acl.ACCESS_PATH)
    acc.allow.append(acl.Edge(src=C, dst=B, user="root"))
    acl.save(acc, acl.ACCESS_PATH)

    live = {}
    overlapped = set()
    seen = []
    lock = threading.Lock()

    def slow_edge(acc, edge, ep, **kw):
        host = ep.target
        with lock:
            seen.append(host)
            for other in live:
                if other != host:
                    overlapped.add(frozenset((other, host)))
                else:
                    raise AssertionError(f"two edges on {host} ran at the same time")
            live[host] = True
        _time.sleep(0.05)
        with lock:
            del live[host]
        return True, ""

    monkeypatch.setattr(rec, "apply_edge", slow_edge)
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(sweep, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert len(seen) >= 2, "the sweep never reached two machines"
    assert overlapped, "the machines were dialled one after another, not at once"


def test_one_unreachable_machine_does_not_discard_the_others(fleet_at, monkeypatch):
    """Found on real hardware. A NAS took longer than the sync timeout to answer, the
    TimeoutExpired came out of the worker, and the whole pass died -- after every other
    machine had already done its work, because a fan-out collects every result before
    using any of them. The serial version at least kept what it had merged."""
    runner, _ = fleet_at
    seen = []

    def flaky(acc, edge, ep, **kw):
        seen.append(ep.target)
        if ep.target == "1.2.3.4":
            raise TimeoutError("this machine never answered")
        return True, ""

    monkeypatch.setattr(rec, "apply_edge", flaky)
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(sweep, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert "5.6.7.8" in seen, "the healthy machine was never reached"
    assert "lin-xps" in r.output, "its result was discarded with the failure"
    assert "oracle not reached" in r.output

    ledger = rec.load_ledger(acl.LEDGER_PATH)
    assert ledger[f"{A}>{C}>root"].observed == "present", "the good edge must still land"
    assert "TimeoutError" in ledger[f"{A}>{B}>root"].last_error


def test_the_center_does_not_hand_the_inventory_to_itself(fleet_at, monkeypatch):
    """`no endpoints` stood in for `the center` and stopped being true the moment the
    center had an address: it opened an SSH connection to itself to hand itself an
    inventory it had just written. On Windows that hung with no timeout."""
    runner, _ = fleet_at
    # give the center an address of its own, as a listening center has
    devices = inv.load()
    for d in devices:
        if d.id == "id:center":
            d.endpoints = [{"target": "center.example", "user": "u", "port": 22}]
    inv.save(devices, inv.INVENTORY_PATH)

    dialled = []
    monkeypatch.setattr(sync, "run_sync", lambda ep, *a, **k: dialled.append(ep.target) or (255, ""))
    # broadcast seals once then sends per machine, so the stub goes
    # on the half that dials; sealing would shell out to ssh-keygen.
    monkeypatch.setattr(sync, "sealed_envelope", lambda payload: payload)
    monkeypatch.setattr(sync, "send_sealed", lambda ep, *a, **k: dialled.append(ep.target) or (255, ""))
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(sweep, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    runner.invoke(cli.app, ["sync"])
    assert dialled, "the sweep never broadcast at all"
    assert "center.example" not in dialled, "the center dialled itself"


def test_the_inventory_is_signed_once_not_once_per_machine(fleet_at, monkeypatch):
    """Found by hanging a real sweep. `broadcast` called `run_sync` per machine, and
    run_sync read the access list, read telemetry out of sqlite and shelled out to
    `ssh-keygen -Y sign` -- none of which varies per machine, and none of which survives
    being run from eight threads at once. A faulthandler dump of the wedged center showed
    the workers sitting in `access.sign` with their subprocess reader threads waiting on
    pipes that never closed.

    Signing once is also just less work: it is the same envelope for everyone."""
    runner, _ = fleet_at
    seals = []
    sent = []

    monkeypatch.setattr(sync, "sealed_envelope",
                        lambda payload: seals.append(payload) or "SEALED")
    monkeypatch.setattr(sync, "send_sealed",
                        lambda ep, sealed: sent.append((ep.target, sealed)) or (255, ""))
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(sweep, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    runner.invoke(cli.app, ["sync"])

    assert len(sent) >= 2, "the fleet has more than one machine to hand it to"
    assert len(seals) == 1, f"signed {len(seals)} times for {len(sent)} machines"
    assert {s for _, s in sent} == {"SEALED"}, "every machine gets the same envelope"
