"""Small promises of the command line: what it refuses, and the words it uses."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.models import Device, Kind
from fleet.state import inventory as inv


@pytest.fixture
def runner():
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT),
              Device(id="id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    return CliRunner()


def test_a_name_must_be_one(runner):
    r = runner.invoke(cli.app, ["edit", "box", "--name", "", "--json"])
    assert r.exit_code == 2
    r = runner.invoke(cli.app, ["edit", "box", "--name", "a b", "--json"])
    assert r.exit_code == 2


def test_ls_of_a_name_that_matches_nothing_fails(runner):
    r = runner.invoke(cli.app, ["ls", "nosuch", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["unknown"] == ["nosuch"]


def test_top_needs_a_positive_interval(runner):
    assert runner.invoke(cli.app, ["top", "-i", "0"]).exit_code == 2


@pytest.mark.parametrize("odd", ["nan", "inf"])
def test_top_refuses_an_interval_that_is_not_a_number_of_seconds(runner, odd):
    """Both passed `interval <= 0` and came back as a traceback from the first tick."""
    r = runner.invoke(cli.app, ["top", "-i", odd])
    assert r.exit_code == 2 and "--interval" in r.output


@pytest.mark.parametrize("gone", ["invite", "join", "sync", "access", "center", "service"])
def test_the_access_commands_are_gone(runner, gone):
    """fleet manages no access any more: these are not hidden, they do not exist."""
    r = runner.invoke(cli.app, [gone, "--help"])
    assert r.exit_code != 0
