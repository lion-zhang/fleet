"""`fleet ssh`: a real terminal, or one command, and nothing in between.

The most-used command in the tool and, until now, the least tested -- the only thing
pinned was that it fails without a name. It is also the easiest to break by accident:
appending anything to argv when no command was asked for turns an interactive shell into
a non-interactive one, and the failure looks like "my shell exits immediately".
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet import inventory as inv
from fleet import store
from fleet.models import Device, Kind


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    inv.save([Device(id="id:x", name="lin-xps", kind=Kind.PERMANENT,
                     endpoints=[{"target": "lin-xps.example.ts.net", "user": "lin",
                                 "port": 22}])],
             inv.INVENTORY_PATH)
    seen = []
    monkeypatch.setattr(os, "execvp", lambda f, a: seen.append((f, a)))
    return seen


def test_no_command_means_a_plain_interactive_shell(box):
    """`fleet ssh lin-xps` is `ssh lin@lin-xps.example.ts.net` and nothing else. ssh
    allocates a tty of its own accord when there is no remote command."""
    CliRunner().invoke(cli.app, ["ssh", "lin-xps"])
    prog, argv = box[-1]
    assert prog == "ssh"
    assert argv[-1] == "lin@lin-xps.example.ts.net", "no remote command is appended"
    assert not any(a.startswith("sh -lc") for a in argv)


def test_a_command_after_the_separator_is_run(box):
    CliRunner().invoke(cli.app, ["ssh", "lin-xps", "--", "nvidia-smi"])
    argv = box[-1][1]
    assert "nvidia-smi" in argv[-1]
    assert ".local/bin" in argv[-1], "PATH is fixed up: a non-login shell will not have it"


def test_the_process_is_replaced_rather_than_wrapped(box):
    """execvp, not subprocess: ssh must own the terminal outright or job control,
    signals and escape sequences all behave subtly wrong."""
    import inspect

    src = inspect.getsource(cli.cmd_ssh)
    assert "execvp" in src
    assert "subprocess.run" not in src


def test_the_fleet_key_is_offered(box, tmp_path, monkeypatch):
    """Otherwise the most-used command connects with a personal key that fleet no longer
    installs anywhere."""
    key = tmp_path / "id_ed25519"
    key.write_text("x")
    monkeypatch.setattr(cli, "FLEET_KEY", key)
    CliRunner().invoke(cli.app, ["ssh", "lin-xps"])
    argv = box[-1][1]
    assert "-i" in argv and str(key) in argv


def test_a_non_default_port_and_a_jump_host_are_carried(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    inv.save([Device(id="id:v", name="vast2", kind=Kind.RENTAL,
                     endpoints=[{"target": "1.2.3.4", "user": "root", "port": 58418,
                                 "jump": "bastion"}])],
             inv.INVENTORY_PATH)
    seen = []
    monkeypatch.setattr(os, "execvp", lambda f, a: seen.append((f, a)))
    CliRunner().invoke(cli.app, ["ssh", "vast2"])
    argv = seen[-1][1]
    assert argv[argv.index("-p") + 1] == "58418"
    assert argv[argv.index("-J") + 1] == "bastion"


def test_a_host_that_rejected_our_key_says_how_to_fix_it(box, monkeypatch):
    """Better than handing you a connection that will just fail."""
    monkeypatch.setattr(cli, "auth_of", lambda *a, **k: "needs_key")
    r = CliRunner().invoke(cli.app, ["ssh", "lin-xps"])
    assert r.exit_code == 2
    assert "Only the center can install one" in r.output
    assert not box, "and it does not try to connect anyway"
