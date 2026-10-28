"""An address that now leads to another machine is not that machine.

VMs cloned from one template share a host key, so when a DHCP lease moved from one to
another the key still matched: `fleet ls` showed the other box's GPUs and jobs under
this name, and `fleet ssh` opened it.
"""

from __future__ import annotations

from fleet import cli
from fleet.models import Device, Kind, ProbeResult, Snapshot, Status
from fleet.ops import doctor, rows
from fleet.state import inventory as inv
from fleet.state import store


def _setup(tmp_path, monkeypatch, devices, answers):
    path = tmp_path / "machines.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(rows, "probe_many",
                        lambda jobs, **kw: {i: answers[i] for i in jobs if i in answers})
    cli._rows(refresh=True)
    conn = store.connect()
    return {d.id: store.latest(conn, d.id)[0] for d in devices}


def _ok(machine_id, hostname):
    return ProbeResult(status=Status.OK, endpoint_used="ssh",
                       snapshot=Snapshot(machine_id=machine_id, hostname=hostname))


def _dev(dev_id, name):
    return Device(id=dev_id, name=name, kind=Kind.PERMANENT,
                  endpoints=[{"target": "10.0.0.5", "user": "me", "port": 22}])


def test_a_reading_from_another_machine_is_not_recorded_as_this_one(tmp_path, monkeypatch):
    devs = [_dev("linux:machine-id:aaa", "lab"), _dev("linux:machine-id:ccc", "home")]
    got = _setup(tmp_path, monkeypatch, devs, {
        "linux:machine-id:aaa": _ok("bbb", "lab-clone"),
        "linux:machine-id:ccc": _ok("ccc", "home"),
    })
    assert got["linux:machine-id:aaa"]["status"] == Status.PROBE_ERROR.value
    assert "lab-clone" in got["linux:machine-id:aaa"]["error_detail"]
    assert got["linux:machine-id:ccc"]["status"] == Status.OK.value


def test_clones_and_unstamped_ids_are_still_themselves(tmp_path, monkeypatch):
    # A clone fleet split off carries its own suffix on the shared machine-id; a `net:`
    # id or one written by hand says nothing about which machine should answer.
    devs = [_dev("linux:machine-id:aaa:node8", "clone"), _dev("net:10.0.0.5:22", "bare"),
            _dev("gpubox", "hand"), _dev("darwin:hwuuid:ABC-1", "mac")]
    got = _setup(tmp_path, monkeypatch, devs, {
        "linux:machine-id:aaa:node8": _ok("aaa", "node8"),
        "net:10.0.0.5:22": _ok("zzz", "x"),
        "gpubox": _ok("zzz", "x"),
        "darwin:hwuuid:ABC-1": _ok("abc-1", "mac"),
    })
    assert {v["status"] for v in got.values()} == {Status.OK.value}


def test_doctor_says_the_address_leads_elsewhere():
    res = rows.same_machine("linux:machine-id:aaa", "lab", _ok("bbb", "lab-clone"))
    v = doctor.diagnose_result(_dev("linux:machine-id:aaa", "lab"), res)
    assert "another machine" in v.why and "cloned" in v.why
    assert any("fleet edit lab --ssh" in f for f in v.fix)
    assert any(f.startswith("if that one is new:  fleet add") for f in v.fix)


def test_doctor_does_not_call_the_other_machine_ok(monkeypatch):
    from fleet.probe import runner
    monkeypatch.setattr(runner, "probe_endpoints", lambda eps, **kw: _ok("bbb", "lab-clone"))
    v = doctor.diagnose(_dev("linux:machine-id:aaa", "lab"), state=None, snap=None)
    assert v.status == Status.PROBE_ERROR.value and "lab-clone" in v.why
