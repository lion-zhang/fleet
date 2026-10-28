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
