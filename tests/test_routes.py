"""A machine reached two ways, one of them down.

`fleet add` of a second route to a known machine merges it in, and the probe tries every
route and keeps the first that answers. So the machine read as up -- while `fleet ssh`,
the `connect` line of `fleet show` and the tunnel of `fleet models` all went to the route
that was down.
"""

from __future__ import annotations

import os
import sys

from typer.testing import CliRunner

from fleet import cli
from fleet.models import Device, Kind, ProbeResult, Snapshot, Status
from fleet.render.view import Detail, connect_view, device_view
from fleet.state import inventory as inv
from fleet.state import store


def _two_routes() -> Device:
    # As `fleet add` leaves them: the merged route is named "primary" too.
    return Device(id="id:gpubox", name="gpubox", kind=Kind.PERMANENT, endpoints=[
        {"target": "10.0.0.5", "user": "me", "port": 22, "name": "primary", "preference": 10},
        {"target": "gpubox.example.ts.net", "user": "me", "port": 22, "name": "primary",
         "preference": 10}])


def test_every_route_has_a_name_of_its_own():
    names = [e.name for e in inv.endpoints_of(_two_routes())]
    assert len(set(names)) == 2 and names[0] == "primary"


def test_the_route_that_answered_last_is_the_one_offered():
    dev = _two_routes()
    second = inv.endpoints_of(dev)[1].name
    ok = {"status": "ok", "endpoint_used": second}
    assert inv.by_use(dev, ok)[0].target == "gpubox.example.ts.net"
    assert connect_view(dev, ok)["ssh_command"] == "ssh me@gpubox.example.ts.net"
    # Nothing answered, or nothing is known: preference order, as before.
    assert inv.by_use(dev, {"status": "timeout", "endpoint_used": second})[0].target == "10.0.0.5"
    assert inv.by_use(dev)[0].target == "10.0.0.5"


def test_the_tunnel_to_a_model_server_takes_the_route_that_answered():
    dev = _two_routes()
    snap = Snapshot(hostname="gpubox").to_dict()
    snap["services"] = [{"port": 9000, "addr": "127.0.0.1", "kind": "llm", "status": "ok",
                         "framework": "vllm", "model_ids": ["m"]}]
    state = {"status": "ok", "last_probe_at": 1, "endpoint_used": inv.endpoints_of(dev)[1].name}
    (server,) = device_view(dev, state, snap, Detail.FULL)["models"]
    assert server["tunnel"].endswith("me@gpubox.example.ts.net")


def test_fleet_ssh_goes_by_the_route_that_answered(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    dev = _two_routes()
    inv.save([dev])
    conn = store.connect()
    store.record(conn, dev.id, ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="gpubox"),
                                           endpoint_used=inv.endpoints_of(dev)[1].name))
    conn.close()
    seen = []
    monkeypatch.setattr(os, "execvp", lambda f, a: seen.append(a))
    CliRunner().invoke(cli.app, ["ssh", "gpubox"])
    assert seen and seen[-1][-1] == "me@gpubox.example.ts.net"


def test_doctor_names_the_route_that_let_it_in(monkeypatch):
    """`fleet doctor` said "✓ gpubox ok · ssh me@10.0.0.5" -- the route that was down."""
    from fleet.ops import doctor
    from fleet.probe import runner

    dev = _two_routes()
    second = inv.endpoints_of(dev)[1].name
    monkeypatch.setattr(runner, "probe_endpoints", lambda eps, **kw: ProbeResult(
        status=Status.OK, snapshot=Snapshot(), endpoint_used=second))
    v = doctor.diagnose(dev, state=None, snap=None)
    assert v.status == "ok" and "gpubox.example.ts.net" in v.route

    failed = ProbeResult(status=Status.REFUSED, endpoint_used=second)
    assert "gpubox.example.ts.net" in doctor.diagnose_result(dev, failed).route


def _recorded_by_second_route() -> Device:
    dev = _two_routes()
    inv.save([dev])
    conn = store.connect()
    store.record(conn, dev.id, ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="gpubox"),
                                           endpoint_used=inv.endpoints_of(dev)[1].name))
    conn.close()
    return dev


def test_edit_writes_machine_yaml_by_the_route_that_answered(monkeypatch):
    """"Could not change gpubox's machine.yaml: ... Connection refused" on a machine
    `fleet ls` showed up, by its other route."""
    from fleet.ops import machinefile

    _recorded_by_second_route()
    went = []

    def change(ep, platform, **kw):
        went.append(ep.target)
        return {}, {"notes": kw.get("note")}

    monkeypatch.setattr(machinefile, "change", change)
    CliRunner().invoke(cli.app, ["edit", "gpubox", "--note", "hi"])
    assert went == ["gpubox.example.ts.net"]


def test_install_and_update_go_by_the_route_that_answered(monkeypatch):
    _recorded_by_second_route()
    went = []
    monkeypatch.setattr(cli, "run_installer",
                        lambda ep, *a, **k: (went.append(ep.target), (0, ""))[1])
    CliRunner().invoke(cli.app, ["install", "gpubox"])
    CliRunner().invoke(cli.app, ["update", "gpubox"])
    assert went == ["gpubox.example.ts.net"] * 2


def test_the_tunnel_takes_the_key_and_jump_host_fleet_uses(tmp_path):
    """It was ssh, the port and the address alone: a rental reached with its own key
    asked for a password it does not have, and one behind a jump host was not found."""
    key = tmp_path / "rental"
    key.write_text("")
    dev = Device(id="d", name="gpu", kind=Kind.RENTAL, endpoints=[
        {"target": "1.2.3.4", "user": "root", "port": 2222,
         "identity": str(key), "jump": "me@bastion"}])
    snap = Snapshot(hostname="gpu").to_dict()
    snap["services"] = [{"port": 8000, "addr": "127.0.0.1", "kind": "llm", "status": "ok",
                         "model_ids": ["m"]},
                        {"port": 8001, "addr": "::1", "kind": "llm", "status": "ok",
                         "model_ids": ["n"]}]
    v4, v6 = device_view(dev, {"status": "ok"}, snap, Detail.FULL)["models"]
    assert v4["tunnel"] == (f"ssh -N -L 8000:127.0.0.1:8000 -p 2222 -i {key} "
                            "-J me@bastion root@1.2.3.4")
    assert v6["tunnel"].startswith("ssh -N -L 8001:[::1]:8001 ")
