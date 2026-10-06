"""Where a center tells machines to dial it back."""

from __future__ import annotations

from fleet.models import Device, Kind
from fleet.ops import sync
from fleet.state import access as acl
from fleet.state import inventory as inv


def _center_with(tmp_path, monkeypatch, endpoints):
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    inv.save([Device(id="id:me", name="mac", kind=Kind.PERMANENT, endpoints=endpoints)],
             inv.INVENTORY_PATH)
    acc = acl.Access(fleet_id="7f3a9c", center="SHA256:me",
                     keys={"SHA256:me": {"name": "mac", "device_id": "id:me"}})
    monkeypatch.setattr("socket.gethostname", lambda: "mac.example.ts.net")
    return acc


def test_a_loopback_endpoint_is_never_advertised(tmp_path, monkeypatch):
    """The center reached as `ssh me@localhost` has a real endpoint called localhost.
    Advertised, it sent every invited machine to dial itself (found on a real runner)."""
    acc = _center_with(tmp_path, monkeypatch, [
        {"target": "localhost", "user": "me", "port": 22, "preference": 1},
        {"target": "127.0.0.1", "user": "me", "port": 22, "preference": 2},
        {"target": "mac.lan", "user": "me", "port": 22, "preference": 3}])
    assert sync.this_host(acc) == "mac.lan"


def test_only_loopback_falls_back_to_the_hostname(tmp_path, monkeypatch):
    acc = _center_with(tmp_path, monkeypatch, [
        {"target": "localhost", "user": "me", "port": 22}])
    assert sync.this_host(acc) == "mac.example.ts.net"
    assert "localhost" not in sync.center_advertise_url(acc)
