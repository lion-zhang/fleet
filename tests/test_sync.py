"""Merging two inventories.

This is the whole correctness surface of sync. Everything else is transport: if the
merge is right, a dropped connection costs you a retry; if the merge is wrong, it
silently eats a device you spent an evening onboarding.
"""

from __future__ import annotations

import subprocess

import pytest

from fleet import config

from fleet.state.inventory import merge, touch
from fleet.models import Device, Kind
from fleet.ops import identity
from fleet.ops import sweep
from fleet.ops import sync


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
    from fleet.state import inventory as inv

    path = tmp_path / "inventory.yaml"
    inv.save([_dev("box", updated_at=12345)], path)
    assert inv.load(path)[0].updated_at == 12345


# --------------------------------------------------------------- serialisation

def test_an_inventory_survives_a_round_trip_through_text():
    """Sync ships inventories over a pipe, so the on-the-wire form must be the same
    thing the file holds -- one format, not two that can drift."""
    from fleet.state.inventory import dumps, loads

    devices = [_dev("a", updated_at=7), _dev("b", notes="hello")]
    back = loads(dumps(devices))
    assert _names(back) == ["a", "b"]
    assert back[0].updated_at == 7
    assert back[1].notes == "hello"


def test_the_wire_format_is_the_file_format(tmp_path):
    from fleet.state import inventory as inv

    path = tmp_path / "inventory.yaml"
    inv.save([_dev("a")], path)
    assert inv.dumps(inv.load(path)).strip() == path.read_text().strip()


# --------------------------------------------------------------- the server side

def _serve_env(tmp_path, monkeypatch, devices):
    from typer.testing import CliRunner

    from fleet.state import inventory as inv, store

    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    _sandbox_access(tmp_path, monkeypatch)
    return CliRunner(), path


def _sandbox_access(tmp_path, monkeypatch):
    """access.py resolves its paths at import, so the FLEET_*_DIR env vars do not reach
    it. Without this a `--serve` test pins a center key into the real config directory
    -- which one of these did, silently, until it was noticed."""
    from fleet.state import access as acl

    for name in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, name, tmp_path / getattr(acl, name).name)


def _sealed(tmp_path, devices):
    """`--serve` only accepts a signed envelope now: it runs on a spoke that every
    granted peer holds a key for, so an unsigned inventory is indistinguishable from a
    hostile one."""
    import subprocess

    from fleet.state import access as acl
    from fleet.state import inventory as inv

    key = tmp_path / "center_key"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                       check=True)
    return acl.seal(inv.dumps(devices), key_path=key)


def test_serve_merges_what_it_is_given_with_what_it_holds(tmp_path, monkeypatch):
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch, [_dev("center-only")])
    incoming = _sealed(tmp_path, [_dev("laptop-only")])
    result = runner.invoke(app, ["sync", "--serve"], input=incoming)
    assert result.exit_code == 0, result.output
    assert _names(inv.loads(result.stdout)) == ["center-only", "laptop-only"]


def test_serve_persists_the_merge_so_the_center_stays_canonical(tmp_path, monkeypatch):
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch, [_dev("center-only")])
    runner.invoke(app, ["sync", "--serve"], input=_sealed(tmp_path, [_dev("laptop-only")]))
    assert _names(inv.load(path)) == ["center-only", "laptop-only"]


def test_serve_rejects_junk_rather_than_destroying_the_inventory(tmp_path, monkeypatch):
    """A truncated pipe must not be read as 'the other side has no devices'."""
    from fleet.state import inventory as inv
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
    monkeypatch.setattr("fleet.ops.identity.local_device_id", lambda: "linux:machine-id:me")
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0


def test_sync_applies_what_the_center_returns(tmp_path, monkeypatch):
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch,
                              [_dev("laptop"), _dev("hub", role="center")])
    returned = inv.dumps([_dev("laptop"), _dev("hub", role="center"), _dev("from-center")])
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: (0, returned))
    # broadcast seals once then sends per machine, so the stub goes
    # on the half that dials; sealing would shell out to ssh-keygen.
    monkeypatch.setattr(sync, "sealed_envelope", lambda payload: payload)
    monkeypatch.setattr(sync, "send_sealed", lambda *a, **k: (0, returned))
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "from-center" in _names(inv.load(path))


def test_a_failed_sync_leaves_local_state_untouched(tmp_path, monkeypatch):
    """Sync is not on the critical path. A dead center must not cost you your inventory."""
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _serve_env(tmp_path, monkeypatch,
                              [_dev("laptop"), _dev("hub", role="center")])
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: (255, "connection refused"))
    # broadcast seals once then sends per machine, so the stub goes
    # on the half that dials; sealing would shell out to ssh-keygen.
    monkeypatch.setattr(sync, "sealed_envelope", lambda payload: payload)
    monkeypatch.setattr(sync, "send_sealed", lambda *a, **k: (255, "connection refused"))
    result = runner.invoke(app, ["sync"])
    assert result.exit_code != 0
    assert _names(inv.load(path)) == ["hub", "laptop"]


# --------------------------------------------------------------- one center only

def test_promoting_a_center_demotes_the_previous_one():
    from fleet.state.inventory import promote_center

    devices = [_dev("old", role="center"), _dev("new")]
    promote_center(devices, devices[1])
    assert [d.role for d in devices] == ["none", "center"]


def test_the_demoted_center_keeps_no_standing_privilege():
    """It still has fleet installed and still holds a full copy. Dropping it to 'none'
    would silently throw away a replica."""
    from fleet.state.inventory import promote_center

    devices = [_dev("old", role="center"), _dev("new")]
    promote_center(devices, devices[1])
    assert devices[0].role == "none"


def test_demotion_is_stamped_so_it_survives_the_next_merge():
    """An unstamped demotion loses to the other machine's stale 'center' record, and
    you are back to two centers."""
    from fleet.state.inventory import promote_center

    old = _dev("old", role="center", updated_at=1)
    devices = [old, _dev("new")]
    promote_center(devices, devices[1])
    assert old.updated_at > 1


def test_devices_that_are_not_brokers_are_left_alone():
    from fleet.state.inventory import promote_center

    # A legacy record may still say "backup" -- the role was removed, but inventories
    # written before that are on disk. Promotion touches only the outgoing center, so a
    # stale value is left exactly as found rather than silently rewritten.
    devices = [_dev("plain"), _dev("legacy", role="backup"), _dev("new")]
    promote_center(devices, devices[2])
    assert devices[0].role == "none"
    assert devices[1].role == "backup", "not ours to change; only the center is demoted"


def test_promoting_the_current_center_again_changes_nothing():
    from fleet.state.inventory import promote_center

    devices = [_dev("hub", role="center")]
    assert promote_center(devices, devices[0]) == []


def test_a_merge_that_produces_two_centers_keeps_only_the_newer(tmp_path):
    """Two machines can each promote a different device before syncing. The merge has
    to resolve that, or `fleet sync` picks a center arbitrarily from then on."""
    local = [_dev("a", role="center", updated_at=100), _dev("b", updated_at=100)]
    remote = [_dev("a", updated_at=100), _dev("b", role="center", updated_at=300)]
    merged, _ = merge(local, remote)
    assert [d.role for d in merged] == ["none", "center"]
    assert sum(1 for d in merged if d.role == "center") == 1


# --------------------------------------------------------------- automatic sync
# --------------------------------------------------------------- deletion

def test_removing_a_device_leaves_a_tombstone_rather_than_a_hole(tmp_path):
    """A record that merely vanishes is indistinguishable from one the other machine
    has not seen yet, so the next sync would resurrect it."""
    from fleet.state import inventory as inv

    path = tmp_path / "inventory.yaml"
    inv.save([_dev("gone"), _dev("kept")], path)
    devices = inv.load(path)
    inv.remove(devices, inv.find(devices, "gone"))
    inv.save(devices, path)

    assert [d.name for d in inv.load(path)] == ["gone", "kept"], "record is still there"
    assert [d.name for d in inv.live(inv.load(path))] == ["kept"], "but not live"


def test_a_tombstoned_device_is_not_found_by_name():
    from fleet.state import inventory as inv

    devices = [_dev("gone"), _dev("kept")]
    inv.remove(devices, devices[0])
    assert inv.find(devices, "gone") is None
    assert inv.find(devices, "kept") is not None


def test_a_deletion_propagates_through_a_merge():
    """Deleted on the laptop, so it must go away on the center too."""
    from fleet.state import inventory as inv

    remote = [_dev("doomed", updated_at=100)]
    local = [_dev("doomed", updated_at=100)]
    inv.remove(local, local[0])
    merged, _ = merge(remote, local)
    assert inv.live(merged) == []


def test_an_older_deletion_does_not_undo_a_newer_re_add():
    """Delete it on the laptop, then add it again on the desktop. The re-add is newer,
    so the device comes back rather than being permanently poisoned."""
    from fleet.state import inventory as inv

    local = [_dev("box", updated_at=100)]
    inv.remove(local, local[0])                        # tombstone, stamped now
    readded = _dev("box", updated_at=int(__import__("time").time()) + 60)
    merged, _ = merge(local, [readded])
    assert [d.name for d in inv.live(merged)] == ["box"]


def test_a_tombstone_survives_the_inventory_file(tmp_path):
    """It has to outlive a restart, or the deletion is forgotten before it propagates."""
    from fleet.state import inventory as inv

    path = tmp_path / "inventory.yaml"
    devices = [_dev("gone")]
    inv.remove(devices, devices[0])
    inv.save(devices, path)
    assert inv.load(path)[0].deleted_at > 0


def test_ancient_tombstones_are_pruned_so_the_file_does_not_grow_forever():
    from fleet.state import inventory as inv

    old = _dev("ancient")
    old.deleted_at = 1                                  # 1970
    fresh = _dev("recent")
    inv.remove([fresh], fresh)
    kept = inv.prune_tombstones([old, fresh])
    assert [d.name for d in kept] == ["recent"]


# --------------------------------------------------------------- re-adding

def test_re_adding_a_removed_device_brings_it_back():
    """`fleet rm` then `fleet add` is an ordinary correction. upsert matches on id, so
    without clearing the tombstone the add merges into a deleted record and the device
    stays invisible -- it looks like `fleet add` silently did nothing."""
    from fleet.state import inventory as inv

    devices = [_dev("box", id="net:box:22")]
    inv.remove(devices, devices[0])
    devices, action = inv.upsert(devices, _dev("box", id="net:box:22"))
    assert [d.name for d in inv.live(devices)] == ["box"]


def test_re_adding_reports_that_it_was_restored():
    """"unchanged" would be a lie: the device was invisible a moment ago."""
    from fleet.state import inventory as inv

    devices = [_dev("box", id="net:box:22")]
    inv.remove(devices, devices[0])
    _, action = inv.upsert(devices, _dev("box", id="net:box:22"))
    assert action == "restored"


def test_a_restored_device_keeps_everything_you_had_recorded_about_it():
    """Keeping the record through a deletion is the whole reason it is a tombstone;
    coming back as a blank device would waste that."""
    from fleet.state import inventory as inv

    devices = [_dev("box", id="net:box:22", notes="the noisy one", tags=["gpu"])]
    inv.remove(devices, devices[0])
    devices, _ = inv.upsert(devices, _dev("box", id="net:box:22"))
    restored = inv.live(devices)[0]
    assert restored.notes == "the noisy one" and restored.tags == ["gpu"]


def test_restoring_stamps_the_record_so_the_center_learns_about_it():
    """Otherwise the next sync sees the center's stale tombstone as newer and deletes
    it right back."""
    from fleet.state import inventory as inv

    devices = [_dev("box", id="net:box:22")]
    inv.remove(devices, devices[0])
    devices[0].updated_at = 1
    devices, _ = inv.upsert(devices, _dev("box", id="net:box:22"))
    assert devices[0].updated_at > 1


def test_adding_a_device_that_was_never_removed_is_unaffected():
    """The dedupe behaviour that already existed must not change."""
    from fleet.state import inventory as inv

    devices = [_dev("box", id="net:box:22")]
    _, action = inv.upsert(devices, _dev("box", id="net:box:22"))
    assert action == "unchanged"


# --------------------------------------------------------------- lost updates

def test_sync_does_not_erase_a_device_added_while_it_was_running(tmp_path, monkeypatch):
    """auto-sync is spawned before the command that triggered it even runs, so a
    `fleet add` lands in the middle of the round trip. Saving the merge computed from
    sync's own stale snapshot silently erases it -- which is how a device that `fleet
    add` and `fleet identity` both confirmed vanished before `fleet ls`.
    """
    from typer.testing import CliRunner

    from fleet import cli
    from fleet.state import inventory as inv, store
    from fleet.cli import app

    path = tmp_path / "inventory.yaml"
    inv.save([_dev("hub", role="center")], path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(identity, "local_device_id", lambda: "linux:machine-id:laptop")

    def racing_center(ep, payload):
        """The center answers -- and `fleet add` commits while we are waiting."""
        concurrent = inv.load(path)
        concurrent.append(_dev("just-added"))
        inv.save(concurrent, path)
        return 0, payload            # center knows nothing of the new device

    monkeypatch.setattr(sync, "run_sync", racing_center)
    # broadcast seals once then sends per machine, so the stub goes
    # on the half that dials; sealing would shell out to ssh-keygen.
    monkeypatch.setattr(sync, "sealed_envelope", lambda payload: payload)
    monkeypatch.setattr(sync, "send_sealed", racing_center)
    result = runner_invoke = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "just-added" in [d.name for d in inv.live(inv.load(path))], \
        "sync overwrote a device committed during its round trip"


def test_the_center_does_not_erase_a_device_added_while_it_was_serving(tmp_path, monkeypatch):
    """--serve has the same window: it loads, merges what arrived, and writes back."""
    from typer.testing import CliRunner

    from fleet import cli
    from fleet.state import inventory as inv, store
    from fleet.cli import app

    path = tmp_path / "inventory.yaml"
    inv.save([_dev("center-only")], path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")

    real_loads = inv.loads

    def loads_then_race(text):
        parsed = real_loads(text)
        concurrent = inv.load(path)
        concurrent.append(_dev("added-on-the-center"))
        inv.save(concurrent, path)
        return parsed

    monkeypatch.setattr(inv, "loads", loads_then_race)
    _sandbox_access(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, ["sync", "--serve"], input=_sealed(tmp_path, [_dev("remote")]))
    assert result.exit_code == 0, result.output
    names = [d.name for d in inv.live(inv.load(path))]
    assert "added-on-the-center" in names, names


# ------------------------------------------------- taking a route away, not just adding

def test_an_endpoint_can_be_removed_by_the_authority():
    """The union ran both ways, so a route one machine learned survived forever on
    every machine however wrong it turned out to be -- there was no deletion primitive
    anywhere. The center's answer replaces rather than merges."""
    from fleet.state import inventory as inv

    mine = _dev("box")
    mine.endpoints = [{"target": "good", "user": "root", "port": 22},
                      {"target": "bad", "user": "root", "port": 22, "preference": 1}]
    theirs = _dev("box")
    theirs.endpoints = [{"target": "good", "user": "root", "port": 22}]
    theirs.updated_at = mine.updated_at + 10

    merged, _ = inv.merge([mine], [theirs], authoritative=True)
    assert [e["target"] for e in merged[0].endpoints] == ["good"]


def test_an_ordinary_merge_still_unions():
    """Between peers a route one side knows is still a real route; only the authority
    may take one away."""
    from fleet.state import inventory as inv

    mine = _dev("box")
    mine.endpoints = [{"target": "lan", "user": "root", "port": 22}]
    theirs = _dev("box")
    theirs.endpoints = [{"target": "mesh", "user": "root", "port": 22}]
    theirs.updated_at = mine.updated_at + 10

    merged, _ = inv.merge([mine], [theirs])
    assert {e["target"] for e in merged[0].endpoints} == {"lan", "mesh"}


# ------------------------------------------- dialling a center that cannot reach us

def test_a_machine_the_center_cannot_reach_can_dial_it_instead(tmp_path, monkeypatch):
    """`ensure_fresh` needs the center's address *and* its key, and both only ever
    arrive in an envelope the center delivers by dialling out. A machine that can reach
    the listener but has never been swept is stuck: pinned in the access list, a member
    in every sense the center cares about, and with no way to find it. Found on a real
    machine whose ssh path from the center had failed."""
    from fleet.state import access as acl
    from fleet.state import inventory as inv
    from fleet.ops import sync
    from fleet.models import Device, Kind

    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    monkeypatch.setattr(config, "FLEET_KEY", key)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    for n in ("ACCESS_PATH", "CACHE_PATH", "LEDGER_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    inv.save([Device(id="id:me", name="me", kind=Kind.PERMANENT)], inv.INVENTORY_PATH)

    assert acl.center_url() == "", "precondition: it does not know where the center is"

    # what the center would answer, sealed with its own key
    centre_key = tmp_path / "centre_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(centre_key)],
                   check=True)
    theirs = inv.dumps([Device(id="id:hub", name="hub", kind=Kind.PERMANENT),
                        Device(id="id:me", name="me", kind=Kind.PERMANENT)])
    reply = acl.seal(theirs, key_path=centre_key,
                     center_url="http://hub.example:7373/sync")
    monkeypatch.setattr(sync, "post", lambda url, payload, **k: reply)

    summary = sync.join("http://typed-by-hand:7373/sync")

    assert "hub" in {d.name for d in inv.load()}, "the center's inventory did not land"
    assert acl.center_url() == "http://hub.example:7373/sync", \
        "it must remember where to ask next time, as the center names it"
    assert acl.trusted_center_pubkey().strip() == \
        centre_key.with_suffix(".pub").read_text().strip(), "the center was not pinned"
    assert "machine" in summary


def test_dialling_counts_machines_not_tombstones(tmp_path, monkeypatch):
    """The summary is the only thing the person who typed the address sees, and a fleet
    that has ever removed a machine keeps the tombstone until every machine has seen it.
    Counting records made a seven-machine fleet report fourteen."""
    from fleet.state import access as acl
    from fleet.state import inventory as inv
    from fleet.ops import sync
    from fleet.models import Device, Kind

    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    monkeypatch.setattr(config, "FLEET_KEY", key)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    for n in ("ACCESS_PATH", "CACHE_PATH", "LEDGER_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    inv.save([Device(id="id:me", name="me", kind=Kind.PERMANENT)], inv.INVENTORY_PATH)

    centre_key = tmp_path / "centre_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(centre_key)],
                   check=True)
    gone = Device(id="id:gone", name="gone", kind=Kind.PERMANENT)
    gone.deleted_at = 1
    theirs = inv.dumps([Device(id="id:hub", name="hub", kind=Kind.PERMANENT),
                        Device(id="id:me", name="me", kind=Kind.PERMANENT), gone])
    reply = acl.seal(theirs, key_path=centre_key, center_url="http://hub.example:7373/sync")
    monkeypatch.setattr(sync, "post", lambda url, payload, **k: reply)

    assert "2 machine(s)" in sync.join("http://typed-by-hand:7373/sync")


def test_dialling_refuses_an_answer_it_cannot_trust(tmp_path, monkeypatch):
    """Once a center is pinned, an answer from anywhere else is refused -- the typed
    address selects who to ask, never who to believe."""
    from fleet.state import access as acl
    from fleet.state import inventory as inv
    from fleet.ops import sync
    from fleet.ops.errors import FleetError
    from fleet.models import Device, Kind

    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    monkeypatch.setattr(config, "FLEET_KEY", key)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    for n in ("ACCESS_PATH", "CACHE_PATH", "LEDGER_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    inv.save([Device(id="id:me", name="me", kind=Kind.PERMANENT)], inv.INVENTORY_PATH)

    real = tmp_path / "real_ed25519"
    impostor = tmp_path / "impostor_ed25519"
    for k in (real, impostor):
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(k)],
                       check=True)
    acl.pin_center_pubkey(real.with_suffix(".pub").read_text())

    forged = acl.seal(inv.dumps([Device(id="id:evil", name="evil", kind=Kind.PERMANENT)]),
                      key_path=impostor)
    monkeypatch.setattr(sync, "post", lambda url, payload, **k: forged)

    with pytest.raises(FleetError):
        sync.join("http://wherever:7373/sync")
    assert "evil" not in {d.name for d in inv.load()}
