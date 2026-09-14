"""Inventory identity, dedupe, and persistence."""

from __future__ import annotations

import pathlib

import pytest

from fleet.state import inventory as inv
from fleet.models import Device, Kind, ProbeResult, Status
from fleet.onboard import classify_kind, derive_id, slugify, suggest_name
from fleet.probe.parse import parse_payload
from fleet.ssh.cmd import Endpoint, parse_ssh_command

FIX = pathlib.Path(__file__).parent / "fixtures" / "probe"


def dev(name: str, id_: str, **kw) -> Device:
    kw.setdefault("endpoints", [{"name": "primary", "target": f"{name}.example", "user": "lin", "port": 22}])
    return Device(id=id_, name=name, **kw)


# ------------------------------------------------------------------ dedupe
def test_same_machine_via_two_addresses_is_one_device():
    """oracle answers as both root@ and ubuntu@, and a tailnet box is usually also on
    the LAN. Those are one machine, and the inventory must say so."""
    mid = "linux:machine-id:22222222222222222222222222222222"
    devices = [dev("vm-a", mid, endpoints=[{"name": "tailnet", "target": "oracle.ts.net", "user": "root", "port": 22}])]
    incoming = dev("oracle-lan", mid, endpoints=[{"name": "lan", "target": "10.0.0.5", "user": "ubuntu", "port": 22}])
    devices, action = inv.upsert(devices, incoming)
    assert action == "endpoint_added"
    assert len(devices) == 1
    assert len(devices[0].endpoints) == 2


def test_readding_identical_endpoint_is_a_noop():
    mid = "linux:machine-id:abc"
    d = dev("x", mid)
    devices, action = inv.upsert([d], dev("x", mid))
    assert action == "unchanged"
    assert len(devices) == 1 and len(devices[0].endpoints) == 1


def test_distinct_machines_stay_distinct():
    devices, _ = inv.upsert([dev("a", "linux:machine-id:aaa")], dev("b", "linux:machine-id:bbb"))
    assert len(devices) == 2


# ------------------------------------------------------------------ identity derivation
def test_machine_id_is_preferred_over_address():
    snap = parse_payload((FIX / "gpu-box.txt").read_text())
    ep = Endpoint(target="gpu-box.example.ts.net", user="lin")
    assert derive_id(snap, ep) == "linux:machine-id:11111111111111111111111111111111"


def test_macos_uses_hardware_uuid_namespace():
    snap = parse_payload((FIX / "macos-laptop.txt").read_text())
    assert derive_id(snap, Endpoint(target="mac")).startswith("darwin:hwuuid:")


def test_unfingerprintable_host_falls_back_to_address():
    """A container with no readable machine-id still needs a stable id."""
    ep = Endpoint(target="5.6.7.8", port=58418)
    assert derive_id(None, ep) == "net:5.6.7.8:58418"


# ------------------------------------------------------------------ classification
def test_slurm_host_is_classified_shared():
    snap = parse_payload((FIX / "gpu-box.txt").read_text())
    snap.slurm = True
    assert classify_kind(snap, Endpoint(target="koa04.seas.upenn.edu")) is Kind.SHARED


def test_many_logged_in_users_implies_shared():
    snap = parse_payload((FIX / "gpu-box.txt").read_text())
    snap.users = 9
    assert classify_kind(snap, Endpoint(target="cluster")) is Kind.SHARED


def test_vast_label_implies_rental():
    snap = parse_payload((FIX / "gpu-box.txt").read_text())
    snap.vast_label = "C.12345678"
    assert classify_kind(snap, Endpoint(target="1.2.3.4")) is Kind.RENTAL


def test_rental_recognised_from_hostname_when_unreachable():
    assert classify_kind(None, Endpoint(target="ssh2.vast.ai")) is Kind.RENTAL


def test_plain_box_is_permanent():
    snap = parse_payload((FIX / "gpu-box.txt").read_text())
    assert classify_kind(snap, Endpoint(target="gpu-box")) is Kind.PERMANENT


# ------------------------------------------------------------------ policy invariants
def test_shared_host_is_never_claimable():
    """A claim on a machine you share with other people is a lie to them."""
    assert dev("koa04", "x", kind=Kind.SHARED).claimable is False
    assert dev("gpu-box", "y", kind=Kind.PERMANENT).claimable is True


def test_on_demand_hosts_are_off_the_automatic_sweep():
    """Verified necessary: probing koa04 without VPN hangs ~75s and would stall `fleet ls`."""
    assert dev("koa04", "x", probe_policy="on_demand").probeable is False
    assert dev("gpu-box", "y", probe_policy="auto").probeable is True
    assert dev("iphone", "z", kind=Kind.MOBILE).probeable is False


def test_shared_host_gets_the_polite_probe_mode():
    assert dev("koa04", "x", kind=Kind.SHARED).probe_mode == "shared"
    assert dev("gpu-box", "y").probe_mode == "full"


# ------------------------------------------------------------------ persistence
def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "inventory.yaml"
    original = [dev("gpu-box", "linux:machine-id:abc", kind=Kind.PERMANENT,
                    notes="free box", tags=["gpu"]),
                dev("koa04", "net:koa04:22", kind=Kind.SHARED, probe_policy="on_demand")]
    inv.save(original, path)
    loaded = {d.name: d for d in inv.load(path)}
    assert set(loaded) == {"gpu-box", "koa04"}
    assert loaded["koa04"].kind is Kind.SHARED
    assert loaded["koa04"].probe_policy == "on_demand"
    assert loaded["gpu-box"].notes == "free box"
    assert loaded["gpu-box"].tags == ["gpu"]


def test_missing_inventory_is_empty_not_an_error(tmp_path):
    assert inv.load(tmp_path / "nope.yaml") == []


def test_malformed_yaml_reports_rather_than_crashes(tmp_path):
    p = tmp_path / "inventory.yaml"
    p.write_text("devices:\n  - name: x\n   bad_indent: y\n")
    with pytest.raises(inv.InventoryError):
        inv.load(p)


def test_endpoints_are_sorted_by_preference():
    d = dev("x", "id", endpoints=[
        {"name": "lan", "target": "10.0.0.1", "user": "lin", "preference": 20},
        {"name": "tailnet", "target": "x.ts.net", "user": "lin", "preference": 10}])
    assert [e.name for e in sorted(inv.endpoints_of(d), key=lambda e: e.preference)] == ["tailnet", "lan"]


# ------------------------------------------------------------------ ssh command parsing
@pytest.mark.parametrize("cmd,target,user,port", [
    ("ssh -p 58418 root@5.6.7.8", "5.6.7.8", "root", 58418),
    ("ssh lin@gpu-box.example.ts.net", "gpu-box.example.ts.net", "lin", None),
    ("ssh ssh2.vast.ai.x8", "ssh2.vast.ai.x8", None, None),
    ("ssh -i ~/.ssh/k -p 2222 u@h", "h", "u", 2222),
    ("ssh -J bastion lin@10.0.0.5", "10.0.0.5", "lin", None),
    ("-p 22 u@h", "h", "u", 22),
])
def test_parse_ssh_command(cmd, target, user, port):
    p = parse_ssh_command(cmd)
    assert (p.target, p.user, p.port) == (target, user, port)


def test_trailing_remote_command_is_not_the_host():
    p = parse_ssh_command("ssh lin@box nvidia-smi -L")
    assert p.target == "box" and p.remote_command.startswith("nvidia-smi")


def test_suggest_name_prefers_hostname_and_uniquifies():
    snap = parse_payload((FIX / "gpu-box.txt").read_text())
    assert suggest_name(snap, Endpoint(target="1.2.3.4"), set()) == "gpu-box"
    assert suggest_name(snap, Endpoint(target="1.2.3.4"), {"gpu-box"}) == "gpu-box-2"


def test_slugify():
    assert slugify("VM-0-8-ubuntu") == "vm-0-8-ubuntu"
    assert slugify("") == "device"


def test_an_fqdn_is_trimmed_to_its_first_label():
    """The trim was written but unreachable: slugify turns every dot into a dash, so
    splitting on "." afterwards never found one. A tailnet host came out as
    "box-tailXXXXXX-ts-net" instead of "box"."""
    from fleet.onboard import suggest_name

    assert suggest_name(None, Endpoint(target="box.tailXXXXXX.ts.net"), set()) == "box"
    assert suggest_name(None, Endpoint(target="ssh2.vast.ai"), set()) == "ssh2"


def test_an_address_is_not_trimmed():
    """The first label of 5.6.7.8 is not a name."""
    from fleet.onboard import suggest_name

    assert suggest_name(None, Endpoint(target="5.6.7.8"), set()) == "5-6-7-8"


def test_a_trimmed_name_still_avoids_collisions():
    from fleet.onboard import suggest_name

    assert suggest_name(None, Endpoint(target="box.tailXXXXXX.ts.net"), {"box"}) == "box-2"
