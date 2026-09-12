"""A usage example on every command.

`--help` is where someone lands having forgotten a command's *shape*, and the shape is
the hard part: which argument is quoted, whether the device comes before or after the
flag, what `--` separates. A list of option names answers none of that. The example sits
inside each command's help rather than in a separate section, so you meet it exactly
where you are already looking.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from fleet.cli import app

runner = CliRunner()

COMMANDS = [
    ("ls",), ("show",), ("add",), ("edit",), ("rm",), ("refresh",), ("probe",),
    ("ssh",), ("setup",), ("paths",), ("top",), ("install",), ("sync",), ("identity",),
    ("key", "install"), ("secret", "set"), ("secret", "ls"), ("secret", "rm"),
    ("access",), ("center",),
]


def _help(*args) -> str:
    result = runner.invoke(app, [*args, "--help"], env={"COLUMNS": "110"})
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: " ".join(c))
def test_every_command_carries_an_example(command):
    out = _help(*command)
    assert "Example:" in out, f"`fleet {' '.join(command)} --help` has no example"


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: " ".join(c))
def test_each_example_is_a_runnable_fleet_invocation(command):
    """An example that does not start with `fleet` is a description, not an example."""
    line = next(ln for ln in _help(*command).splitlines() if "Example:" in ln)
    assert re.search(r"Example:\s+fleet ", line), line


def test_the_examples_are_not_collected_into_a_separate_section():
    """They belong beside each command, not in one list nobody scrolls to."""
    assert "Examples" not in _help()


def test_no_example_leaks_a_real_host_address():
    """Help text gets copied and pasted, so it must not teach one of the user's own
    boxes -- the probe fixtures already carry a sanitisation guard for this reason."""
    out = _help() + "".join(_help(*c) for c in COMMANDS)
    for found in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", out):
        assert found.startswith(("1.2.3.4", "5.6.7.8", "10.", "192.168.", "127.")), found
