"""Deploying the newest fleet from git.

`fleet install` already re-ran as an update, one device at a time and always over ssh.
The two things missing were the ones you actually reach for.
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet import install
from fleet.ops import identity
from fleet.state import inventory as inv
from fleet.state import store
from fleet.models import Device, Kind


@pytest.fixture
def fleet_of(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:me")
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


# ------------------------------------------- update is not install, and not one shell

def test_a_device_with_no_fleet_is_skipped_rather_than_given_one(fleet_of, monkeypatch):
    """`fleet update --all` reaches every device with a route, and most of a fleet is
    meant to have nothing installed -- the probe is a script piped over one connection.
    Deploying a fix therefore also installed fleet on a NAS and on whatever rental
    happened to answer. The device is the authority on whether it has fleet, so the
    script asks and exits 91, and the caller reports it as skipped rather than failed."""
    runner, _, _ = fleet_of
    monkeypatch.setattr(cli, "run_installer",
                        lambda ep, script, **kw: (install.NOTHING_TO_UPDATE, ""))
    r = runner.invoke(cli.app, ["update", "--all"])
    assert r.exit_code == 0, "a machine that was never meant to run fleet is not a failure"
    assert "1 skipped" in r.output
    assert "fleet install oracle" in r.output, "it says how to install one on purpose"


def test_the_update_script_refuses_where_there_is_nothing_to_update():
    """Both shells, because a fleet is not one platform -- and before uv is fetched, so
    a skipped device is left exactly as it was found."""
    for platform in ("", "windows"):
        guarded = install.install_script("https://e/f.git", platform=platform,
                                         update_only=True)
        assert str(install.NOTHING_TO_UPDATE) in guarded
        assert guarded.index(str(install.NOTHING_TO_UPDATE)) < guarded.index("uv"), \
            "the check has to come before uv is fetched onto a machine we are skipping"
        assert str(install.NOTHING_TO_UPDATE) not in \
            install.install_script("https://e/f.git", platform=platform), \
            "`fleet install` still installs: that is what it is for"


def test_a_windows_device_is_updated_with_the_windows_installer(fleet_of, monkeypatch):
    """`fleet install` had always read the platform from the last probe and `fleet
    update` had not, so updating a Windows center handed it the POSIX script. sh.exe is
    there because git is, so it ran -- far enough to stop fleet by running fleet, which
    holds open the directory uv then fails to remove. That leaves no working fleet on
    the machine at all. Found on a real center, which had to be repaired by hand."""
    import json

    runner, _, _ = fleet_of
    devices = inv.load(inv.INVENTORY_PATH)
    devices.append(Device(id="id:win", name="beelink", kind=Kind.PERMANENT,
                          endpoints=[{"target": "win.example", "user": "zl", "port": 22}]))
    inv.save(devices, inv.INVENTORY_PATH)
    conn = store.connect()
    conn.execute("INSERT INTO snapshot (device_id, ts, source, payload) VALUES (?,?,?,?)",
                 ("id:win", 1, "self", json.dumps({"uname_s": "Windows"})))
    conn.commit()
    conn.close()

    seen = {}
    monkeypatch.setattr(cli, "run_installer",
                        lambda ep, script, **kw: seen.update(script=script, kw=kw) or (0, ""))
    assert runner.invoke(cli.app, ["update", "beelink"]).exit_code == 0
    assert seen["kw"].get("platform") == "windows", "ssh was told the wrong shell"
    assert "$ErrorActionPreference" in seen["script"], "it was handed the POSIX script"


def test_this_machine_is_updated_in_the_shell_it_actually_runs(fleet_of, monkeypatch):
    """The local half had the same bug with none of the ssh: `sh -c` on a Windows center
    is git's sh.exe, where the POSIX script half-runs. The script goes over stdin so the
    payload is bytes either way -- text mode rewrites \\n to \\r\\n on Windows."""
    runner, _, local = fleet_of
    assert runner.invoke(cli.app, ["update"]).exit_code == 0
    assert local[0] == ["sh", "-s"], "posix: the script arrives on stdin, not as argv"

    monkeypatch.setattr(install, "local_platform", lambda: "windows")
    local.clear()
    runner.invoke(cli.app, ["update"])
    assert local[0][0] == "powershell"
    assert "-Command" in local[0] and "-" not in local[0], \
        "`powershell -Command -` evaluates stdin statement by statement and breaks blocks"


def test_running_from_a_checkout_skips_this_machine_rather_than_failing(fleet_of,
                                                                        monkeypatch):
    """The same answer the remote half gives, for the machine you are sitting at."""
    runner, _, _ = fleet_of
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(
                            argv, install.NOTHING_TO_UPDATE, b"", b""))
    r = runner.invoke(cli.app, ["update"])
    assert r.exit_code == 0
    assert "no installed fleet" in r.output
