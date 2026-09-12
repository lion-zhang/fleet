"""Deploying the newest fleet from git.

`fleet install` already re-ran as an update, one device at a time and always over ssh.
The two things missing were the ones you actually reach for.
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet import inventory as inv
from fleet import store
from fleet.models import Device, Kind


@pytest.fixture
def fleet_of(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cli, "local_device_id", lambda: "id:me")
    monkeypatch.setattr(cli, "configured_repo", lambda: "git@example.com:me/fleet.git")
    inv.save([
        Device(id="id:me", name="macbook", kind=Kind.PERMANENT,
               endpoints=[{"target": "me.example", "user": "lin", "port": 22}]),
        Device(id="id:oracle", name="oracle", kind=Kind.PERMANENT,
               endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}]),
        Device(id="id:nas", name="ds720", kind=Kind.APPLIANCE),      # no endpoint
    ], inv.INVENTORY_PATH)

    remote, local = [], []
    monkeypatch.setattr(cli, "run_installer",
                        lambda ep, script, **kw: remote.append(ep.target) or (0, "ok"))
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: local.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "fleet 0.4.0", ""))
    return CliRunner(), remote, local


def test_updating_this_machine_does_not_go_over_ssh(fleet_of):
    """The center is never an ssh target, so connecting to ourselves would fail on
    exactly the machine most likely to be running the command."""
    runner, remote, local = fleet_of
    r = runner.invoke(cli.app, ["update"])
    assert r.exit_code == 0, r.output
    assert not remote, "it dialled out to update the machine it was already on"
    assert local and local[0][0] == "sh"


def test_updating_everything_reaches_every_device_with_a_route(fleet_of):
    runner, remote, local = fleet_of
    r = runner.invoke(cli.app, ["update", "--all"])
    assert r.exit_code == 0, r.output
    assert remote == ["1.2.3.4"], "ds720 has no endpoint; this machine is done locally"
    assert local, "and this machine is still updated, without ssh"


def test_one_unreachable_device_does_not_stop_the_rest(fleet_of, monkeypatch):
    """A fleet half-updated on purpose beats one half-updated by an exception."""
    runner, remote, _ = fleet_of
    monkeypatch.setattr(cli, "run_installer", lambda ep, script, **kw: (1, "no route"))
    r = runner.invoke(cli.app, ["update", "--all"])
    assert r.exit_code == 1, "it reports failure"
    assert "1 updated, 1 failed" in r.output
    assert "oracle" in r.output


def test_a_named_device_is_updated_over_ssh(fleet_of):
    runner, remote, local = fleet_of
    r = runner.invoke(cli.app, ["update", "oracle"])
    assert r.exit_code == 0, r.output
    assert remote == ["1.2.3.4"]
    assert not local, "and this machine is left alone"


def test_no_repo_configured_says_what_to_set(fleet_of, monkeypatch):
    runner, _, _ = fleet_of
    monkeypatch.setattr(cli, "configured_repo", lambda: "")
    r = runner.invoke(cli.app, ["update"])
    assert r.exit_code == 2
    assert "--repo" in r.output and "config.yaml" in r.output
