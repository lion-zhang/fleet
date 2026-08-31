"""Usage examples in --help.

`--help` is where someone lands when they have forgotten the shape of a command, and
the shape is the part that is hard to remember: which argument is quoted, whether the
device comes before or after the flag, what `--` separates. A list of option names does
not answer any of those; a worked example does.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fleet.cli import app

runner = CliRunner()


def _help(*args) -> str:
    result = runner.invoke(app, [*args, "--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.output
    return result.output


def test_the_top_level_help_shows_examples():
    out = _help()
    assert "Examples" in out


def test_the_examples_show_a_real_onboarding_command():
    """The one command whose shape nobody remembers: the whole ssh invocation, quoted."""
    assert "fleet add" in _help()


@pytest.mark.parametrize("command", ["add", "edit", "ssh", "install", "top", "secret"])
def test_each_non_obvious_command_carries_its_own_example(command):
    out = _help(command)
    assert "Examples" in out, f"`fleet {command} --help` has no example"
    assert "fleet " + command in out


def test_examples_do_not_contain_a_real_host_address():
    """Help text is copied and pasted. It must not teach one of the user's own boxes,
    and the fixtures already have a sanitisation guard for exactly this reason."""
    import re

    out = _help() + "".join(
        _help(c) for c in ("add", "edit", "ssh", "install", "top", "secret"))
    for found in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", out):
        assert found.startswith(("1.2.3.4", "5.6.7.8", "10.", "192.168.", "127.")), found
