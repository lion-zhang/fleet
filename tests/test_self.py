"""Recognising the machine fleet is running on.

Requiring sshd, a key in authorized_keys and a working network path in order to look at
the machine fleet is *already running on* is a lot of moving parts for no extra
information. It is also the only reason inbound SSH had to be enabled on a laptop.
"""

from __future__ import annotations

import sys

import pytest

from fleet.models import Device, Kind, Status
from fleet.probe.runner import run_probe_local
from fleet.view import Detail, device_view

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")

STATE_OK = {"status": "ok", "last_probe_at": 10**9, "last_ok_at": 10**9}


def _dev(name="me", id="linux:machine-id:me") -> Device:
    return Device(id=id, name=name, kind=Kind.PERMANENT)


# --------------------------------------------------------------- local probe

def test_probing_locally_returns_a_real_snapshot():
    res = run_probe_local()
    assert res.status is Status.OK, res.error_detail
    assert res.snapshot is not None and res.snapshot.os


def test_the_local_probe_needs_no_endpoint_at_all():
    """That is the point: no ssh, no key, no network."""
    res = run_probe_local()
    assert res.endpoint_used == "local"


def test_configured_disk_paths_are_honoured_locally(tmp_path):
    res = run_probe_local(disk_paths=[str(tmp_path)])
    assert [d.mount for d in res.snapshot.disks] == [str(tmp_path)]


def test_the_shared_mode_is_honoured_locally():
    assert run_probe_local(mode="shared").status is Status.OK


def test_a_local_probe_failure_comes_back_as_a_result_not_an_exception():
    """Every other probe path promises this; the loop around it cannot special-case one."""
    res = run_probe_local(timeout=0.0001)
    assert res.status is not Status.OK
    assert isinstance(res.error_detail, str)


# --------------------------------------------------------------- the view

def test_the_current_machine_is_marked_in_the_view():
    v = device_view(_dev(), STATE_OK, None, Detail.COMPACT, self_id="linux:machine-id:me")
    assert v["is_self"] is True


def test_other_machines_are_not_marked():
    v = device_view(_dev(), STATE_OK, None, Detail.COMPACT, self_id="linux:machine-id:other")
    assert v["is_self"] is False


def test_nothing_is_marked_when_we_cannot_tell_which_machine_we_are():
    """A container without /etc/machine-id has no identity; claiming to be a device
    would be worse than admitting we do not know."""
    v = device_view(_dev(), STATE_OK, None, Detail.COMPACT, self_id="")
    assert v["is_self"] is False


def test_a_device_with_no_id_is_never_mistaken_for_this_machine():
    v = device_view(_dev(id=""), STATE_OK, None, Detail.COMPACT, self_id="")
    assert v["is_self"] is False


# --------------------------------------------------------------- the sweep

def test_the_current_machine_is_not_probed_over_ssh(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fleet import cli, inventory as inv, store
    from fleet.cli import app

    devices = [_dev("me", "linux:machine-id:me"), _dev("other", "linux:machine-id:other")]
    for d in devices:
        d.endpoints = [{"target": f"{d.name}.example", "user": "root", "port": 22}]
    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cli, "maybe_autosync", lambda: None)
    monkeypatch.setattr(cli, "local_device_id", lambda: "linux:machine-id:me")

    over_ssh = []
    monkeypatch.setattr(cli, "probe_many",
                        lambda jobs, **kw: over_ssh.extend(jobs) or {})
    locally = []
    monkeypatch.setattr(cli, "run_probe_local",
                        lambda **kw: locally.append(1) or ProbeResultOK())

    CliRunner().invoke(app, ["ls", "--refresh"])
    assert over_ssh == ["linux:machine-id:other"], over_ssh
    assert len(locally) == 1


def ProbeResultOK():
    from fleet.models import ProbeResult, Snapshot
    return ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="me"))
