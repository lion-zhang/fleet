"""machines.yaml shared with dotfiles: one list, read on machines whose homes differ."""

from __future__ import annotations

from pathlib import Path

from fleet.models import Device
from fleet.onboard import endpoint_dict
from fleet.ssh.cmd import Endpoint
from fleet.state import inventory as inv


def _home(monkeypatch, path: Path) -> Path:
    path.joinpath(".ssh").mkdir(parents=True, exist_ok=True)
    path.joinpath(".ssh", "work").write_text("key", encoding="utf-8")
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


def test_a_key_in_your_home_is_recorded_from_your_home(tmp_path, monkeypatch):
    # `-i ~/.ssh/work` was recorded as /home/lin/.ssh/work: on a Mac (/Users/lin) the
    # list then named a key that is not there, and fleet went in without it.
    here = _home(monkeypatch, tmp_path / "linux-home")
    rec = endpoint_dict(Endpoint(target="h", identity=str(here / ".ssh" / "work")))
    assert rec["identity"] == "~/.ssh/work"
    elsewhere = str(tmp_path / "keys" / "k")
    assert endpoint_dict(Endpoint(target="h", identity=elsewhere))["identity"] == elsewhere

    there = _home(monkeypatch, tmp_path / "mac-home")
    (ep,) = inv.endpoints_of(Device(id="id:g", name="gpu", endpoints=[rec]))
    assert Path(ep.identity) == there / ".ssh" / "work"


def test_the_same_key_spelled_out_is_the_same_route(tmp_path, monkeypatch):
    here = _home(monkeypatch, tmp_path / "home")
    old = {"target": "h", "user": "me", "port": 22, "identity": "~/.ssh/work"}
    assert inv.same_route({**old, "identity": str(here / ".ssh" / "work")}, old)
    assert not inv.same_route({**old, "identity": str(tmp_path / "other")}, old)
