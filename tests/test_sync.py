"""Merging two inventories.

This is the whole correctness surface of sync. Everything else is transport: if the
merge is right, a dropped connection costs you a retry; if the merge is wrong, it
silently eats a device you spent an evening onboarding.
"""

from __future__ import annotations

from fleet.inventory import merge, touch
from fleet.models import Device, Kind


def _dev(name: str, *, id: str | None = None, updated_at: int = 100, **kw) -> Device:
    kw.setdefault("endpoints", [{"target": f"{name}.example", "user": "root", "port": 22}])
    return Device(id=id or f"linux:machine-id:{name}", name=name,
                  kind=kw.pop("kind", Kind.PERMANENT), updated_at=updated_at, **kw)


def _names(devices: list[Device]) -> list[str]:
    return sorted(d.name for d in devices)


# --------------------------------------------------------------- union

def test_a_device_only_the_remote_knows_about_is_adopted():
    merged, _ = merge([_dev("a")], [_dev("b")])
    assert _names(merged) == ["a", "b"]


def test_a_device_only_the_local_side_knows_about_survives():
    """Sync must never be a download that discards local work."""
    merged, _ = merge([_dev("local-only")], [])
    assert _names(merged) == ["local-only"]


def test_an_empty_remote_does_not_wipe_the_local_inventory():
    """A center that has just been set up is empty. That is not a delete-everything
    instruction."""
    merged, _ = merge([_dev("a"), _dev("b")], [])
    assert _names(merged) == ["a", "b"]


# --------------------------------------------------------------- conflicts

def test_the_newer_record_wins():
    local = _dev("box", updated_at=100, notes="old")
    remote = _dev("box", updated_at=200, notes="new")
    merged, _ = merge([local], [remote])
    assert merged[0].notes == "new"


def test_an_older_remote_does_not_clobber_newer_local_work():
    local = _dev("box", updated_at=300, notes="just edited here")
    remote = _dev("box", updated_at=200, notes="stale")
    merged, _ = merge([local], [remote])
    assert merged[0].notes == "just edited here"


def test_devices_are_matched_on_id_not_name():
    """Two machines may have named the same box differently; the machine-id is what
    makes them the same box."""
    local = _dev("gpu", id="linux:machine-id:same", updated_at=100)
    remote = _dev("gpu-box", id="linux:machine-id:same", updated_at=200)
    merged, _ = merge([local], [remote])
    assert len(merged) == 1


# --------------------------------------------------------------- endpoints

def test_endpoints_from_both_sides_are_kept():
    """One machine reaches the box on the tailnet, another on the LAN. Same box, two
    routes, and losing either makes it unreachable from one of your machines."""
    local = _dev("box", updated_at=100,
                 endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])
    remote = _dev("box", updated_at=200,
                  endpoints=[{"target": "box.example.ts.net", "user": "root", "port": 22}])
    merged, _ = merge([local], [remote])
    targets = {e["target"] for e in merged[0].endpoints}
    assert targets == {"10.0.0.5", "box.example.ts.net"}


def test_the_same_endpoint_on_both_sides_is_not_duplicated():
    ep = {"target": "box.example", "user": "root", "port": 22}
    merged, _ = merge([_dev("box", endpoints=[ep])], [_dev("box", endpoints=[dict(ep)])])
    assert len(merged[0].endpoints) == 1


def test_endpoints_differing_only_by_user_are_both_kept():
    """`oracle` answers as both root@ and ubuntu@ -- inventory.upsert already treats
    those as distinct routes to one machine."""
    local = _dev("box", endpoints=[{"target": "h", "user": "root", "port": 22}])
    remote = _dev("box", endpoints=[{"target": "h", "user": "ubuntu", "port": 22}])
    merged, _ = merge([local], [remote])
    assert len(merged[0].endpoints) == 2


# --------------------------------------------------------------- properties

def test_merge_is_deterministic_regardless_of_input_order():
    a, b = _dev("a"), _dev("b")
    assert _names(merge([a], [b])[0]) == _names(merge([b], [a])[0])


def test_merging_an_inventory_with_itself_changes_nothing():
    """Sync runs repeatedly. The second run must be a no-op, or every sync manufactures
    a change and pushes it back."""
    devices = [_dev("a"), _dev("b")]
    _, changes = merge(devices, [_dev("a"), _dev("b")])
    assert changes == []


def test_merge_reports_what_it_did():
    _, changes = merge([], [_dev("newbox")])
    assert any("newbox" in c for c in changes)


# --------------------------------------------------------------- updated_at

def test_touch_advances_updated_at():
    dev = _dev("box", updated_at=100)
    touch(dev)
    assert dev.updated_at > 100


def test_updated_at_survives_the_inventory_file(tmp_path):
    """A timestamp that does not persist cannot resolve tomorrow's conflict."""
    from fleet import inventory as inv

    path = tmp_path / "inventory.yaml"
    inv.save([_dev("box", updated_at=12345)], path)
    assert inv.load(path)[0].updated_at == 12345


# --------------------------------------------------------------- serialisation

def test_an_inventory_survives_a_round_trip_through_text():
    """Sync ships inventories over a pipe, so the on-the-wire form must be the same
    thing the file holds -- one format, not two that can drift."""
    from fleet.inventory import dumps, loads

    devices = [_dev("a", updated_at=7), _dev("b", notes="hello")]
    back = loads(dumps(devices))
    assert _names(back) == ["a", "b"]
    assert back[0].updated_at == 7
    assert back[1].notes == "hello"


def test_the_wire_format_is_the_file_format(tmp_path):
    from fleet import inventory as inv

    path = tmp_path / "inventory.yaml"
    inv.save([_dev("a")], path)
    assert inv.dumps(inv.load(path)).strip() == path.read_text().strip()


# --------------------------------------------------------------- the server side

def _serve_env(tmp_path, monkeypatch, devices):
    from typer.testing import CliRunner

    from fleet import inventory as inv, store

    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    return CliRunner(), path


def test_serve_merges_what_it_is_given_with_what_it_holds(tmp_path, monkeypatch):
    from fleet import inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch, [_dev("center-only")])
    incoming = inv.dumps([_dev("laptop-only")])
    result = runner.invoke(app, ["sync", "--serve"], input=incoming)
    assert result.exit_code == 0, result.output
    assert _names(inv.loads(result.stdout)) == ["center-only", "laptop-only"]


def test_serve_persists_the_merge_so_the_center_stays_canonical(tmp_path, monkeypatch):
    from fleet import inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch, [_dev("center-only")])
    runner.invoke(app, ["sync", "--serve"], input=inv.dumps([_dev("laptop-only")]))
    assert _names(inv.load(path)) == ["center-only", "laptop-only"]


def test_serve_rejects_junk_rather_than_destroying_the_inventory(tmp_path, monkeypatch):
    """A truncated pipe must not be read as 'the other side has no devices'."""
    from fleet import inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch, [_dev("precious")])
    result = runner.invoke(app, ["sync", "--serve"], input="{{{ not yaml")
    assert result.exit_code != 0
    assert _names(inv.load(path)) == ["precious"]


# --------------------------------------------------------------- the client side

def test_sync_says_so_when_no_center_is_designated(tmp_path, monkeypatch):
    from fleet.cli import app

    runner, _ = _serve_env(tmp_path, monkeypatch, [_dev("a")])
    result = runner.invoke(app, ["sync"])
    assert result.exit_code != 0
    assert "center" in result.output.lower()


def test_sync_on_the_center_itself_is_a_no_op_not_an_error(tmp_path, monkeypatch):
    """Running the same command everywhere should be safe; the center has nobody to
    ask."""
    from fleet.cli import app

    runner, _ = _serve_env(tmp_path, monkeypatch, [_dev("me", role="center")])
    monkeypatch.setattr("fleet.cli.local_device_id", lambda: "linux:machine-id:me")
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0


def test_sync_applies_what_the_center_returns(tmp_path, monkeypatch):
    from fleet import cli, inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch,
                              [_dev("laptop"), _dev("hub", role="center")])
    returned = inv.dumps([_dev("laptop"), _dev("hub", role="center"), _dev("from-center")])
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: (0, returned))
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "from-center" in _names(inv.load(path))


def test_a_failed_sync_leaves_local_state_untouched(tmp_path, monkeypatch):
    """Sync is not on the critical path. A dead center must not cost you your inventory."""
    from fleet import cli, inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch,
                              [_dev("laptop"), _dev("hub", role="center")])
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: (255, "connection refused"))
    result = runner.invoke(app, ["sync"])
    assert result.exit_code != 0
    assert _names(inv.load(path)) == ["hub", "laptop"]


# --------------------------------------------------------------- one center only

def test_promoting_a_center_demotes_the_previous_one():
    from fleet.inventory import promote_center

    devices = [_dev("old", role="center"), _dev("new")]
    promote_center(devices, devices[1])
    assert [d.role for d in devices] == ["backup", "center"]


def test_the_demoted_center_becomes_a_backup_not_a_bystander():
    """It still has fleet installed and still holds a full copy. Dropping it to 'none'
    would silently throw away a replica."""
    from fleet.inventory import promote_center

    devices = [_dev("old", role="center"), _dev("new")]
    promote_center(devices, devices[1])
    assert devices[0].role == "backup"


def test_demotion_is_stamped_so_it_survives_the_next_merge():
    """An unstamped demotion loses to the other machine's stale 'center' record, and
    you are back to two centers."""
    from fleet.inventory import promote_center

    old = _dev("old", role="center", updated_at=1)
    devices = [old, _dev("new")]
    promote_center(devices, devices[1])
    assert old.updated_at > 1


def test_devices_that_are_not_brokers_are_left_alone():
    from fleet.inventory import promote_center

    devices = [_dev("plain"), _dev("backup-node", role="backup"), _dev("new")]
    promote_center(devices, devices[2])
    assert devices[0].role == "none"
    assert devices[1].role == "backup"


def test_promoting_the_current_center_again_changes_nothing():
    from fleet.inventory import promote_center

    devices = [_dev("hub", role="center")]
    assert promote_center(devices, devices[0]) == []


def test_a_merge_that_produces_two_centers_keeps_only_the_newer(tmp_path):
    """Two machines can each promote a different device before syncing. The merge has
    to resolve that, or `fleet sync` picks a center arbitrarily from then on."""
    local = [_dev("a", role="center", updated_at=100), _dev("b", updated_at=100)]
    remote = [_dev("a", updated_at=100), _dev("b", role="center", updated_at=300)]
    merged, _ = merge(local, remote)
    assert [d.role for d in merged] == ["backup", "center"]
    assert sum(1 for d in merged if d.role == "center") == 1


# --------------------------------------------------------------- automatic sync

def _auto_env(tmp_path, monkeypatch, devices, **cfg):
    from fleet import cli, inventory as inv, store

    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cli, "local_device_id", lambda: "linux:machine-id:me")
    settings = {"auto_sync": True, "sync_ttl_s": 300, **cfg}
    monkeypatch.setattr(cli, "load_config", lambda: type("C", (), {
        "get": staticmethod(lambda k: settings.get(k))})())
    spawned = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    return spawned


def test_a_stale_fleet_syncs_itself_without_being_asked(tmp_path, monkeypatch):
    from fleet import cli

    spawned = _auto_env(tmp_path, monkeypatch, [_dev("hub", role="center")])
    cli.maybe_autosync()
    assert spawned, "a stale inventory should sync on its own"


def test_a_recently_synced_fleet_does_not_sync_again(tmp_path, monkeypatch):
    """Otherwise every command pays for an SSH round trip."""
    from fleet import cli

    spawned = _auto_env(tmp_path, monkeypatch, [_dev("hub", role="center")])
    cli.maybe_autosync()
    spawned.clear()
    cli.maybe_autosync()
    assert not spawned


def test_the_center_does_not_sync_to_itself(tmp_path, monkeypatch):
    from fleet import cli

    spawned = _auto_env(tmp_path, monkeypatch,
                        [_dev("me", id="linux:machine-id:me", role="center")])
    cli.maybe_autosync()
    assert not spawned


def test_a_fleet_with_no_center_does_not_try(tmp_path, monkeypatch):
    from fleet import cli

    spawned = _auto_env(tmp_path, monkeypatch, [_dev("plain")])
    cli.maybe_autosync()
    assert not spawned


def test_auto_sync_can_be_switched_off(tmp_path, monkeypatch):
    from fleet import cli

    spawned = _auto_env(tmp_path, monkeypatch, [_dev("hub", role="center")],
                        auto_sync=False)
    cli.maybe_autosync()
    assert not spawned


def test_a_broken_auto_sync_never_breaks_the_command_you_ran(tmp_path, monkeypatch):
    """It is a background convenience. `fleet ls` must still work with a dead center,
    an unreadable config, or no network at all."""
    from fleet import cli

    _auto_env(tmp_path, monkeypatch, [_dev("hub", role="center")])
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    cli.maybe_autosync()          # must not raise
