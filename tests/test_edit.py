"""Editing a device after onboarding.

Two things make this more than a setter. Addresses are load-bearing: a device that was
never successfully probed has no machine-id, so `net:<host>:<port>` IS its identity and
changing the address changes who it is. And the cache is keyed by that id, so a rename
that forgets the cache silently orphans every snapshot the device ever recorded.
"""

from __future__ import annotations

import pytest

from fleet.edit import apply_edits
from fleet.models import Device, Kind
from fleet.ssh.cmd import Endpoint


def _dev(id: str = "net:1.2.3.4:22", **kw) -> Device:
    kw.setdefault("endpoints", [{"target": "1.2.3.4", "user": "root",
                                 "port": 22, "name": "default", "preference": 10}])
    return Device(id=id, name=kw.pop("name", "box"), **kw)


def _ep(target: str = "5.6.7.8", port: int = 2222, user: str = "root") -> Endpoint:
    return Endpoint(target=target, user=user, port=port)


# --------------------------------------------------------------- addresses

def test_a_new_ssh_command_replaces_the_primary_address():
    dev = _dev()
    apply_edits(dev, endpoint=_ep())
    assert dev.endpoints[0]["target"] == "5.6.7.8"
    assert dev.endpoints[0]["port"] == 2222


def test_replacing_an_address_keeps_the_endpoint_name_and_preference():
    """The endpoint is the same route to the same box; only where it points changed."""
    dev = _dev(endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22,
                           "name": "public", "preference": 5, "via": "public"}])
    apply_edits(dev, endpoint=_ep())
    assert dev.endpoints[0]["name"] == "public"
    assert dev.endpoints[0]["preference"] == 5
    assert dev.endpoints[0]["via"] == "public"


@pytest.mark.parametrize("via", ["mesh", "tailscale"])
def test_other_endpoints_are_left_untouched(via):
    """An overlay route does not stop working because the public IP was recycled.

    Parametrised over the legacy spelling too: inventories written before `via` was
    de-vendored say "tailscale", they are on disk right now, and reading one as a direct
    route would rewrite the single address that never moves."""
    dev = _dev(endpoints=[
        {"target": "box.example.ts.net", "user": "root", "port": 22,
         "name": "overlay", "preference": 1, "via": via},
        {"target": "1.2.3.4", "user": "root", "port": 22, "name": "public", "preference": 10},
    ])
    apply_edits(dev, endpoint=_ep())
    assert dev.endpoints[0]["target"] == "box.example.ts.net", "preferred route survives"
    assert dev.endpoints[1]["target"] == "5.6.7.8"


def test_a_tailnet_only_device_still_gets_its_address_replaced():
    """The tailnet-preferring rule must not make an edit silently do nothing."""
    dev = _dev(endpoints=[{"target": "box.example.ts.net", "user": "root", "port": 22,
                           "name": "overlay", "preference": 1, "via": "mesh"}])
    apply_edits(dev, endpoint=_ep())
    assert dev.endpoints[0]["target"] == "5.6.7.8"


def test_a_device_with_no_endpoint_yet_gains_one():
    dev = _dev(endpoints=[])
    apply_edits(dev, endpoint=_ep())
    assert dev.endpoints[0]["target"] == "5.6.7.8"


# --------------------------------------------------------------- identity

def test_a_never_probed_device_migrates_its_id_to_the_new_address():
    """`net:` means we never got a machine-id, so the address IS the identity. Leaving
    the old id would strand the device under a name that no longer resolves."""
    dev = _dev(id="net:1.2.3.4:22")
    apply_edits(dev, endpoint=_ep())
    assert dev.id == "net:5.6.7.8:2222"


def test_a_probed_device_keeps_its_machine_id_when_the_address_changes():
    """This is the whole reason machine-id is preferred: rentals recycle addresses, and
    the device's history must survive that."""
    dev = _dev(id="linux:machine-id:1111")
    apply_edits(dev, endpoint=_ep())
    assert dev.id == "linux:machine-id:1111"


def test_apply_edits_reports_an_id_migration_so_the_cli_can_move_the_cache():
    """The old id is transient edit state, not something to persist on the device."""
    dev = _dev(id="net:1.2.3.4:22")
    result = apply_edits(dev, endpoint=_ep())
    assert result.previous_id == "net:1.2.3.4:22"
    assert any("net:5.6.7.8:2222" in c for c in result.changes)


def test_no_migration_is_reported_when_the_identity_did_not_move():
    dev = _dev(id="linux:machine-id:1111")
    assert apply_edits(dev, endpoint=_ep()).previous_id is None


# --------------------------------------------------------------- disk paths

def test_disk_paths_are_recorded_on_the_device():
    dev = _dev()
    apply_edits(dev, disk_paths=["/workspace", "/data"])
    assert dev.disk_paths == ["/workspace", "/data"]


def test_clearing_disk_paths_returns_the_device_to_autodetection():
    dev = _dev(disk_paths=["/workspace"])
    apply_edits(dev, disk_paths=[])
    assert dev.disk_paths == []


def test_editing_only_disk_paths_leaves_the_address_alone():
    dev = _dev()
    apply_edits(dev, disk_paths=["/data"])
    assert dev.endpoints[0]["target"] == "1.2.3.4"


def test_an_edit_that_changes_nothing_reports_nothing():
    dev = _dev()
    assert apply_edits(dev).changes == []


def test_disk_paths_survive_a_round_trip_through_the_inventory_file(tmp_path):
    """A setting that does not persist is not a setting."""
    from fleet.state import inventory as inv

    path = tmp_path / "inventory.yaml"
    dev = _dev(kind=Kind.RENTAL)
    apply_edits(dev, disk_paths=["/workspace"])
    inv.save([dev], path)
    assert inv.load(path)[0].disk_paths == ["/workspace"]


# --------------------------------------------------------------- cache migration

def test_renaming_a_device_carries_its_cached_history(tmp_path):
    """Migrating the id without moving the cache would silently orphan every snapshot
    the device recorded -- the device would look brand new instead of moved."""
    from fleet.state import store
    from fleet.models import ProbeResult, Snapshot, Status

    conn = store.connect(tmp_path / "cache.db")
    store.record(conn, "net:1.2.3.4:22",
                 ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="box")))

    store.rename_device(conn, "net:1.2.3.4:22", "net:5.6.7.8:2222")

    state, snap = store.latest(conn, "net:5.6.7.8:2222")
    assert state is not None and snap["hostname"] == "box"
    assert store.latest(conn, "net:1.2.3.4:22") == (None, None)
    conn.close()


def test_renaming_onto_an_id_that_already_has_history_does_not_crash(tmp_path):
    """device_state.device_id is a primary key, so a naive UPDATE would raise. The new
    identity's own history is the truthful one and wins."""
    from fleet.state import store
    from fleet.models import ProbeResult, Snapshot, Status

    conn = store.connect(tmp_path / "cache.db")
    store.record(conn, "old", ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="old")))
    store.record(conn, "new", ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="new")))

    store.rename_device(conn, "old", "new")

    state, snap = store.latest(conn, "new")
    assert state is not None
    assert store.latest(conn, "old") == (None, None)
    conn.close()


# --------------------------------------------------------------- CLI wiring

def _cli_env(tmp_path, monkeypatch, dev: Device):
    """Point the CLI at a scratch inventory and cache, and keep DNS out of it."""
    from typer.testing import CliRunner

    from fleet import cli
    from fleet.state import inventory as inv, store

    path = tmp_path / "inventory.yaml"
    inv.save([dev], path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cli, "resolve_command", lambda cmd, **kw: _ep())
    return CliRunner(), path


def test_cli_edit_rewrites_the_address_in_the_inventory(tmp_path, monkeypatch):
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _cli_env(tmp_path, monkeypatch, _dev(id="linux:machine-id:1111"))
    result = runner.invoke(app, ["edit", "box", "--ssh", "ssh -p 2222 root@5.6.7.8"])
    assert result.exit_code == 0, result.output
    assert inv.load(path)[0].endpoints[0]["target"] == "5.6.7.8"


def test_cli_edit_moves_cached_history_when_the_identity_migrates(tmp_path, monkeypatch):
    """The end-to-end case: a rental that was never reachable gets a new address."""
    from fleet.state import store
    from fleet.cli import app
    from fleet.models import ProbeResult, Snapshot, Status

    runner, _ = _cli_env(tmp_path, monkeypatch, _dev(id="net:1.2.3.4:22"))
    conn = store.connect(tmp_path / "cache.db")
    store.record(conn, "net:1.2.3.4:22",
                 ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="box")))
    conn.close()

    result = runner.invoke(app, ["edit", "box", "--ssh", "ssh -p 2222 root@5.6.7.8"])
    assert result.exit_code == 0, result.output

    conn = store.connect(tmp_path / "cache.db")
    state, snap = store.latest(conn, "net:5.6.7.8:2222")
    assert state is not None and snap["hostname"] == "box", "history followed the device"
    conn.close()


def test_cli_edit_sets_disk_paths(tmp_path, monkeypatch):
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _cli_env(tmp_path, monkeypatch, _dev())
    result = runner.invoke(app, ["edit", "box", "--disk-path", "/workspace",
                                 "--disk-path", "/data"])
    assert result.exit_code == 0, result.output
    assert inv.load(path)[0].disk_paths == ["/workspace", "/data"]


def test_cli_edit_reports_when_there_is_nothing_to_do(tmp_path, monkeypatch):
    from fleet.cli import app

    runner, _ = _cli_env(tmp_path, monkeypatch, _dev())
    result = runner.invoke(app, ["edit", "box"])
    assert result.exit_code == 0
    assert "nothing" in result.output.lower()


def test_cli_edit_fails_clearly_on_an_unknown_device(tmp_path, monkeypatch):
    from fleet.cli import app

    runner, _ = _cli_env(tmp_path, monkeypatch, _dev())
    result = runner.invoke(app, ["edit", "nosuchbox", "--disk-path", "/data"])
    assert result.exit_code != 0


def test_an_edit_stamps_updated_at_so_sync_can_break_the_tie():
    """Without this the merge cannot tell an edited record from a stale one."""
    dev = _dev()
    dev.updated_at = 1
    apply_edits(dev, disk_paths=["/data"])
    assert dev.updated_at > 1


def test_an_edit_that_changed_nothing_does_not_stamp():
    """A no-op edit must not make this machine's copy spuriously win a merge."""
    dev = _dev()
    dev.updated_at = 1
    apply_edits(dev)
    assert dev.updated_at == 1


def test_a_device_can_be_renamed():
    """The name is a label; the id is what merge and the access list key on. So renaming
    is safe and needs no cascade -- and it is the only way to fix a bad one, since
    `fleet add` restores a tombstoned record under the name it already had."""
    dev = _dev()
    before = dev.id
    out = apply_edits(dev, name="lin-beelink")
    assert dev.name == "lin-beelink"
    assert dev.id == before, "identity does not move with the label"
    assert any("name" in c for c in out.changes)


def test_renaming_to_a_taken_name_is_refused():
    dev = _dev()
    with pytest.raises(ValueError, match="already answers to"):
        apply_edits(dev, name="oracle", taken={"oracle"})


def test_renaming_to_the_same_name_is_a_no_op():
    """A no-op edit must not stamp updated_at, or this machine's copy spuriously wins
    the next merge."""
    dev = _dev()
    stamp = dev.updated_at
    out = apply_edits(dev, name=dev.name)
    assert not out.changes
    assert dev.updated_at == stamp
