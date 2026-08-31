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
