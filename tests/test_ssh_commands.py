"""Reading the ssh command you pasted the way ssh itself reads it.

A rental provider's command is pasted whole -- `ssh -p 41234 root@1.2.3.4 -L 8080:...`
-- and fleet keeps what decides how to reach the machine. Each test here is a command
that was read differently from how ssh reads it, or a mistake in one that ended in a
traceback or in a confident answer about the wrong port.
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.install import needs_agent
from fleet.ssh import cmd
from fleet.ssh.cmd import parse_ssh_command


def test_options_straight_after_the_host_are_ssh_s():
    p = parse_ssh_command("ssh root@1.2.3.4 -p 2222 -L 8080:localhost:8080")
    assert (p.target, p.user, p.port, p.remote_command) == ("1.2.3.4", "root", 2222, "")


def test_the_remote_command_s_own_flags_are_not_read_as_ssh_s():
    """`-i 0` here is nvidia-smi's GPU index; read as ssh's, it was a key named "0"."""
    p = parse_ssh_command("ssh box nvidia-smi -i 0")
    assert p.identity is None and p.remote_command == "nvidia-smi -i 0"
    assert parse_ssh_command("ssh box -- ls -l").remote_command == "ls -l"


def test_grouped_switches_still_find_the_port():
    assert parse_ssh_command("ssh -Ap 2222 box").port == 2222


@pytest.mark.parametrize("command, says", [
    ("ssh -p 22x root@h", "is not a port"),
    ("ssh -p 70000 root@h", "is not a port"),
    ("ssh -p", "needs a value"),
    ("ssh -A", "no address"),
])
def test_a_command_ssh_would_refuse_is_refused(command, says):
    """`-p 22x` dialled port 22 and then said nothing listened on "that port"."""
    with pytest.raises(ValueError, match=says):
        parse_ssh_command(command)


def test_a_key_given_as_an_option_is_kept(tmp_path, monkeypatch):
    """`-o IdentityFile=K` is the same key as `-i K`. It was used to resolve the address
    and then left out of the record, so fleet never offered it."""
    key = tmp_path / "vast"
    key.write_text("x")
    monkeypatch.setattr(cmd.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "hostname 1.2.3.4\nuser root\nport 22\n", ""))
    ep = cmd.resolve_command(f"ssh -o IdentityFile={key} root@1.2.3.4")
    assert ep.identity == str(key)


def _ssh_g_refuses(monkeypatch):
    monkeypatch.setattr(cmd.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 255, "", "command-line line 0: Bad port 'abc'."))


def test_add_says_what_ssh_refused_in_one_line(monkeypatch):
    _ssh_g_refuses(monkeypatch)
    r = CliRunner().invoke(cli.app, ["add", "ssh -o Port=abc host"])
    assert r.exit_code == 2 and "Bad port" in r.output
    assert isinstance(r.exception, SystemExit)        # not a traceback
    r = CliRunner().invoke(cli.app, ["add", "--json", "ssh -o Port=abc host"])
    assert r.exit_code == 2 and '"ok": false' in r.stdout


def test_edit_says_what_ssh_refused_in_one_line(monkeypatch):
    from fleet.models import Device
    from fleet.state import inventory as inv

    inv.save([Device(id="id:b", name="box",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    _ssh_g_refuses(monkeypatch)
    r = CliRunner().invoke(cli.app, ["edit", "box", "--ssh", "ssh -o Port=abc host"])
    assert r.exit_code == 2 and "Bad port" in r.output
    assert isinstance(r.exception, SystemExit)


@pytest.mark.parametrize("name", ["a b", "a\nb", "#lab", " "])
def test_hide_refuses_what_the_list_cannot_hold(name):
    """The list is a name a line with `#` comments: `a\\nb` hid two machines, and `#lab`
    hid none, both without a word."""
    r = CliRunner().invoke(cli.app, ["ls", "--discover", "--hide", name])
    assert r.exit_code == 2


def test_unhiding_a_name_that_is_not_hidden_says_so(monkeypatch):
    from fleet.ops import discover

    monkeypatch.setattr(discover, "candidates", lambda **k: [])
    r = CliRunner().invoke(cli.app, ["ls", "--discover", "--unhide", "lba"])
    assert r.exit_code == 0 and "not hidden: lba" in r.output


@pytest.mark.parametrize("url, forwards", [
    ("https://github.com/sinolion/fleet.git", False),
    ("git@github.com:you/fleet.git", True),
    ("ssh://git@example.com/you/fleet.git", True),
    ("/srv/fleet", False),
])
def test_the_agent_goes_only_where_a_clone_needs_it(url, forwards):
    """Forwarding it lets whoever is root on a rental use your keys while the install
    runs, and an https clone has no use for them."""
    assert needs_agent(url) is forwards
    assert needs_agent(url, from_git=False) is False


def test_install_forwards_the_agent_only_for_an_ssh_clone(monkeypatch):
    from fleet.models import Device
    from fleet.state import inventory as inv

    inv.save([Device(id="id:b", name="box",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    seen = []
    monkeypatch.setattr(cli, "run_installer",
                        lambda ep, script, *, forward_agent, platform: (seen.append(forward_agent), (0, "ok"))[1])
    for url in ("https://example.com/fleet.git", "git@example.com:me/fleet.git"):
        assert CliRunner().invoke(cli.app, ["install", "box", "--repo", url]).exit_code == 0
    CliRunner().invoke(cli.app, ["install", "box", "--repo", "https://example.com/f.git",
                                 "--forward-agent"])
    assert seen == [False, True, True]


@pytest.mark.parametrize("command", [
    "ssh -o ProxyCommand='cloudflared access ssh --hostname %h' me@box.example.com",
    "ssh -oproxycommand=nc\\ %h\\ %p box",
])
def test_a_proxycommand_is_refused_rather_than_dropped(command, monkeypatch):
    """Only the address, user, port, key and jump host are recorded. A ProxyCommand was
    dropped without a word, and the machine read as "DNS lookup failed"."""
    from fleet.ssh import cmd

    monkeypatch.setattr(cmd.subprocess, "run", lambda *a, **k: pytest.fail("ssh was run"))
    with pytest.raises(ValueError, match="ProxyCommand.*~/.ssh/config"):
        cmd.resolve(parse_ssh_command(command))


@pytest.mark.parametrize("command,user,target,port", [
    ("ssh ssh://root@1.2.3.4:40001", "root", "1.2.3.4", 40001),
    ("ssh://root@1.2.3.4", "root", "1.2.3.4", None),
    ("ssh://box.example:2222/", None, "box.example", 2222),
    ("ssh://me@[2001:db8::7]:2222", "me", "2001:db8::7", 2222),
    ("ssh -p 22 ssh://me@h:2222", "me", "h", 22),
])
def test_an_ssh_uri_is_read_as_ssh_reads_it(command, user, target, port):
    """ssh takes `ssh://user@host:port`. Split on its last `@` like a plain
    destination, the user came out as `ssh://root` and the address as `host:2222`."""
    p = parse_ssh_command(command)
    assert (p.user, p.target, p.port) == (user, target, port)


@pytest.mark.parametrize("command,user,target,port", [
    ("root@1.2.3.4:2222", "root", "1.2.3.4", 2222),
    ("ssh me@box.example:40001 nvidia-smi", "me", "box.example", 40001),
    ("ssh -p 22 me@h:2222", "me", "h", 22),
    ("me@2001:db8::7", "me", "2001:db8::7", None),
    ("lab", None, "lab", None),
    ("$ ssh -p 2222 root@1.2.3.4", "root", "1.2.3.4", 2222),
    ("# ssh me@h", "me", "h", None),
])
def test_a_port_after_a_colon_is_the_port(command, user, target, port):
    """The way scp and rental dashboards write it. ssh looked `1.2.3.4:2222` up as a
    name and the machine read as "off, asleep, or not on a network". A command copied
    with its prompt took the `$` for the machine."""
    p = parse_ssh_command(command)
    assert (p.user, p.target, p.port) == (user, target, port)


@pytest.mark.parametrize("command,says", [
    ("me@", "no address after the @"),
    ("ssh @", "no address after the @"),
    ("me@host:abc", "write the port as -p"),
    ("me@host:99999", "write the port as -p"),
])
def test_an_address_ssh_cannot_reach_is_said_so(command, says):
    with pytest.raises(ValueError, match=says):
        parse_ssh_command(command)
