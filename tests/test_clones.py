"""Machines cloned from one image share a machine-id. fleet keeps them apart, and the
clone learns the id it was given.

Found in review: telling them apart relied on a hostname stored in cache.db -- clones
that kept the image's hostname, or a machine whose cache was cleared, merged them into
one record -- and the id given to the clone existed only where it was added, so fleet on
the clone still believed it was the machine it was copied from.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.models import Device, Kind, ProbeResult, Snapshot, Status
from fleet.onboard import derive_id
from fleet.ops import identity
from fleet.ssh.cmd import Endpoint
from fleet.ssh.keys import remote_device_id_command
from fleet.state import inventory as inv

BASE = "linux:machine-id:same"


def _snap(host: str, *, booted_ago: int, device_id: str = "") -> Snapshot:
    now = int(time.time())
    return Snapshot(ts=now, hostname=host, machine_id="same", uptime_s=booted_ago,
                    device_id=device_id, uname_s="Linux")


@pytest.fixture
def known(monkeypatch):
    inv.save([Device(id=BASE, name="web", kind=Kind.PERMANENT,
                     endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    written = []
    monkeypatch.setattr(cli, "_run_script",
                        lambda ep, script, **k: written.append((ep.target, script)) or (True, "ok"))
    return written


def _add(monkeypatch, *, new: Snapshot, old_now: Snapshot | None, target="10.0.0.6"):
    monkeypatch.setattr(cli, "onboard", lambda cmd, **k: (
        Device(id=derive_id(new, Endpoint(target=target)), name="web-2",
               kind=Kind.PERMANENT,
               endpoints=[{"target": target, "user": "root", "port": 22}]),
        ProbeResult(status=Status.OK, snapshot=new)))
    monkeypatch.setattr(cli, "run_probe", lambda ep, **k: (
        ProbeResult(status=Status.OK, snapshot=old_now) if old_now
        else ProbeResult(status=Status.TIMEOUT)))
    r = CliRunner().invoke(cli.app, ["add", f"ssh root@{target}"])
    assert r.exit_code == 0, r.output
    return r, inv.live(inv.load())


def test_clones_with_the_same_hostname_are_told_apart_by_when_they_booted(known, monkeypatch):
    r, devices = _add(monkeypatch, new=_snap("web", booted_ago=600),
                      old_now=_snap("web", booted_ago=86400))
    assert len(devices) == 2, [d.id for d in devices]
    clone = next(d for d in devices if d.id != BASE)
    assert clone.id.startswith(BASE + ":")
    assert "cloned from the same image" in r.output
    # and the id is written on the clone, at the address it was added by
    assert known and known[0][0] == "10.0.0.6" and clone.id in known[0][1]


def test_one_box_on_two_addresses_is_one_machine(known, monkeypatch):
    _, devices = _add(monkeypatch, new=_snap("web", booted_ago=600),
                      old_now=_snap("web", booted_ago=605))
    assert [d.id for d in devices] == [BASE]
    assert len(inv.endpoints_of(devices[0])) == 2
    assert not known, "nothing written on a machine that is not a clone"


def test_a_machine_that_moved_is_not_taken_for_a_clone(known, monkeypatch):
    """Its old address now answers as another machine: a rental whose IP was recycled."""
    stranger = Snapshot(ts=int(time.time()), hostname="other", machine_id="else",
                        uptime_s=5, uname_s="Linux")
    _, devices = _add(monkeypatch, new=_snap("web", booted_ago=600), old_now=stranger)
    assert [d.id for d in devices] == [BASE]


def test_when_the_known_machine_is_off_its_last_hostname_decides(known, monkeypatch):
    from fleet.state import store

    conn = store.connect()
    store.record(conn, BASE, ProbeResult(status=Status.OK, snapshot=_snap("web", booted_ago=9)))
    conn.close()
    _, devices = _add(monkeypatch, new=_snap("web-b", booted_ago=600), old_now=None)
    assert {d.id for d in devices} == {BASE, f"{BASE}:web-b"}


def test_a_clone_that_carries_its_id_is_recognised_without_splitting_again(known, monkeypatch):
    inv.save(inv.load() + [Device(id=f"{BASE}:a1b2c3", name="web-2", kind=Kind.PERMANENT,
                                  endpoints=[{"target": "10.0.0.6", "user": "root",
                                              "port": 22}])])
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: pytest.fail("re-probed"))
    _, devices = _add(monkeypatch, new=_snap("web", booted_ago=600, device_id=f"{BASE}:a1b2c3"),
                      old_now=None)
    assert {d.id for d in devices} == {BASE, f"{BASE}:a1b2c3"}


def test_an_id_counts_only_when_it_extends_the_machines_own():
    ep = Endpoint(target="x")
    assert derive_id(_snap("h", booted_ago=1, device_id=f"{BASE}:x1"), ep) == f"{BASE}:x1"
    # copied into an image whose machine-id was then regenerated: describes another box
    assert derive_id(_snap("h", booted_ago=1, device_id="linux:machine-id:old:x1"), ep) == BASE


def test_this_machine_knows_the_id_it_was_given(tmp_path, monkeypatch):
    from fleet import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(identity, "_machine_id", lambda: BASE)
    assert identity.local_device_id() == BASE
    identity.adopt_id(f"{BASE}:x1")
    assert identity.local_device_id() == f"{BASE}:x1"
    (tmp_path / "device-id").write_text("linux:machine-id:old:x1\n")
    assert identity.local_device_id() == BASE, "not this machine's"
    identity.adopt_id(BASE)
    assert not (tmp_path / "device-id").exists()


def test_the_remote_command_takes_only_an_id():
    with pytest.raises(ValueError):
        remote_device_id_command("x; rm -rf ~")
    assert "device-id" in remote_device_id_command(f"{BASE}:x1", platform="windows")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_the_id_written_is_the_id_the_probe_reads(tmp_path):
    from fleet.probe.runner import PAYLOAD

    env = {**os.environ, "HOME": str(tmp_path)}
    p = subprocess.run(["sh", "-c", remote_device_id_command(f"{BASE}:x1")],
                       capture_output=True, text=True, env=env)
    assert p.stdout.strip() == "ok", p.stderr
    out = subprocess.run(["sh", str(PAYLOAD)], capture_output=True, text=True, env=env,
                         timeout=120).stdout
    assert f"host.device_id={BASE}:x1" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_clones_that_share_a_home_each_read_their_own_id(tmp_path, monkeypatch):
    """Containers on one volume, nodes on one NFS home: the file is the same for all of
    them, and the one id in it was read by each -- the next `fleet add` from another
    machine took one clone for the other, and every add stacked another suffix."""
    from fleet import config
    from fleet.probe.runner import PAYLOAD

    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    for host in ("node8", "node9", "node8"):     # node8 written again: still one line
        p = subprocess.run(["sh", "-c", remote_device_id_command(f"{BASE}:{host}", host=host)],
                           capture_output=True, text=True, env=env)
        assert p.stdout.strip() == "ok", p.stderr
    folder = home / ("Library/Application Support/fleet" if sys.platform == "darwin"
                     else ".config/fleet")
    assert len((folder / "device-id").read_text().splitlines()) == 2

    def probed_as(host: str) -> str:
        bin_ = tmp_path / host
        bin_.mkdir(exist_ok=True)
        (bin_ / "hostname").write_text(f"#!/bin/sh\necho {host}\n")
        (bin_ / "hostname").chmod(0o755)
        out = subprocess.run(["sh", str(PAYLOAD)], capture_output=True, text=True,
                             env={**env, "PATH": f"{bin_}:{os.environ['PATH']}",
                                  "FLEET_MODE": "light"}, timeout=120).stdout
        return next(line.split("=", 1)[1] for line in out.splitlines()
                    if line.startswith("host.device_id="))

    assert probed_as("node8") == f"{BASE}:node8"
    assert probed_as("node9") == f"{BASE}:node9"
    assert probed_as("node7") == "", "the clone it was copied from is the machine-id's own"

    monkeypatch.setattr(config, "CONFIG_DIR", folder)
    monkeypatch.setattr(identity, "_machine_id", lambda: BASE)
    import socket
    for host, want in (("node9", f"{BASE}:node9"), ("node7", BASE)):
        monkeypatch.setattr(socket, "gethostname", lambda h=host: h)
        assert identity.local_device_id() == want
