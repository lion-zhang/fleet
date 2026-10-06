"""The first run: a machine in no fleet becomes its center, and never a member that
was meant to be one."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.models import Device, Kind, ProbeResult, Status
from fleet.ops import firstrun
from fleet.state import access as acl


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A machine with fleet installed and nothing else: the auto-center switched on."""
    import fleet.onboard

    monkeypatch.delenv("FLEET_NO_AUTO_CENTER", raising=False)
    monkeypatch.setattr(fleet.onboard, "onboard_self", lambda **k: (
        Device(id="id:me", name="laptop", kind=Kind.PERMANENT), ProbeResult(status=Status.OK)))
    taught = []
    monkeypatch.setattr(firstrun, "teach_agents", lambda: taught.append(1) or ["claude"])
    return taught


@pytest.mark.parametrize("argv", [["ls"], ["show"], ["access"], ["center"], ["ls", "--json"]])
def test_the_first_real_command_starts_a_fleet_here(fresh, argv):
    r = CliRunner().invoke(cli.app, argv)
    assert acl.is_center(), r.output
    assert fresh == [1], "and teaches the agents, once"


@pytest.mark.parametrize("argv", [["--version"], ["sync", "--serve"], ["center", "--leave"],
                                  ["center", "--accept"], ["setup", "--refresh"],
                                  ["paths"], ["center", "--pubkey"], ["ls", "--help"]])
def test_the_commands_that_make_a_member_never_do(fresh, argv):
    """A member turning itself into a center is how a second, competing fleet is born."""
    CliRunner().invoke(cli.app, argv, input="")
    assert not acl.ACCESS_PATH.exists(), argv


def test_a_member_is_left_alone(fresh):
    acl.pin_center_pubkey("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEfO2BAfm3M8YGY39abf7HwQ9kNkKOR7nesP3z7/PatB c")
    CliRunner().invoke(cli.app, ["ls"])
    assert not acl.ACCESS_PATH.exists()


def test_it_can_be_switched_off(fresh, monkeypatch):
    monkeypatch.setenv("FLEET_NO_AUTO_CENTER", "1")
    CliRunner().invoke(cli.app, ["ls"])
    assert not acl.ACCESS_PATH.exists()


def test_it_happens_once(fresh):
    CliRunner().invoke(cli.app, ["ls"])
    CliRunner().invoke(cli.app, ["ls"])
    assert fresh == [1]


def test_a_handover_is_not_mistaken_for_a_status_check(fresh):
    CliRunner().invoke(cli.app, ["center", "gpu-box"])
    assert not acl.ACCESS_PATH.exists()
