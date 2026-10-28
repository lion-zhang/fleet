"""Turning what you typed into an endpoint: what `ssh -G` says, and what fleet keeps.

fleet promises to connect exactly as your own ssh does. Two things it kept from `ssh -G`
broke that: the first default key that existed, which pinned every machine to it with
IdentitiesOnly, and the HostName behind an ssh config alias, which dialled the machine
by an address that no longer matched the alias's Host block.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fleet.models import Device, Kind
from fleet.ssh import cmd
from fleet.state import inventory as inv


def _ssh_g(monkeypatch, lines: str):
    """`ssh -G` answering with `lines`, and the argv it was asked with."""
    asked = []

    def fake_run(argv, **kw):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0, lines, "")

    monkeypatch.setattr(cmd.subprocess, "run", fake_run)
    return asked


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    return tmp_path


def test_a_default_key_ssh_lists_is_not_pinned(home, monkeypatch):
    """ssh -G lists the default identity files whether or not they were asked for. The
    first that existed was recorded, and build_argv then passed it with IdentitiesOnly=yes:
    a key that lives only in your agent was never offered, and `fleet add` said the
    machine refused you while `ssh` let you in."""
    key = home / ".ssh" / "id_ed25519"
    key.write_text("x")
    _ssh_g(monkeypatch, "hostname 10.0.0.7\nuser ubuntu\nport 22\n"
                        f"identityfile ~/.ssh/id_rsa\nidentityfile {key}\n")
    ep = cmd.resolve_command("ssh ubuntu@10.0.0.7")
    assert ep.identity == ""
    assert "IdentitiesOnly=yes" not in cmd.build_argv(ep, multiplex=False)


def test_a_key_named_with_i_is_still_pinned(home, monkeypatch):
    key = home / "keys" / "vast"
    key.parent.mkdir()
    key.write_text("x")
    _ssh_g(monkeypatch, f"hostname 1.2.3.4\nuser root\nport 40001\nidentityfile {key}\n")
    ep = cmd.resolve_command(f"ssh -p 40001 -i {key} root@1.2.3.4")
    assert ep.identity == str(key)
    argv = cmd.build_argv(ep, multiplex=False)
    assert argv[argv.index("-i") + 1] == str(key) and "IdentitiesOnly=yes" in argv


def test_a_default_key_an_older_inventory_recorded_is_dropped(home):
    """Inventories written before this fix name ~/.ssh/id_ed25519 for nearly every
    machine. ssh offers that key anyway; pinned, it shut out the agent's."""
    default, own = home / ".ssh" / "id_ed25519", home / ".ssh" / "vast_key"
    default.write_text("x")
    own.write_text("x")
    dev = Device(id="id:a", name="a", kind=Kind.PERMANENT, endpoints=[
        {"target": "h1", "identity": str(default)},
        {"target": "h2", "identity": str(own)},
    ])
    got = {e.target: e.identity for e in inv.endpoints_of(dev)}
    assert got == {"h1": "", "h2": str(own)}


def test_an_ssh_config_alias_is_dialled_by_its_name(home, monkeypatch):
    """`Host box` with a ProxyCommand: `ssh box` works, but fleet dialled the HostName,
    which matches no Host block, so the ProxyCommand was never used -- "DNS lookup
    failed" for a machine you reach every day."""
    _ssh_g(monkeypatch, "hostname box.internal\nuser lin\nport 22\n"
                        "proxycommand cloudflared access ssh --hostname %h\n")
    ep = cmd.resolve_command("ssh box")
    assert ep.target == "box" and ep.host == "box.internal"
    assert ep.address == "box.internal"
    assert cmd.build_argv(ep, multiplex=False)[-2] == "lin@box"
    assert ep.ssh_command() == "ssh lin@box"


def test_a_name_ssh_config_leaves_alone_has_no_separate_host(home, monkeypatch):
    _ssh_g(monkeypatch, "hostname 10.0.0.7\nuser ubuntu\nport 22\n")
    ep = cmd.resolve_command("ssh ubuntu@10.0.0.7")
    assert (ep.target, ep.host, ep.address) == ("10.0.0.7", "", "10.0.0.7")


def test_an_alias_is_recorded_with_its_address(home, monkeypatch):
    """The alias is what ssh is given; the address is what a model server's URL and the
    route kind need, since an HTTP client has never heard of ~/.ssh/config."""
    from fleet import onboard
    from fleet.models import ProbeResult, Status
    from fleet.render.view import _host_of

    _ssh_g(monkeypatch, "hostname 192.168.1.20\nuser lin\nport 22\n")
    monkeypatch.setattr(onboard, "run_probe",
                        lambda ep, timeout: ProbeResult(status=Status.UNREACHABLE))
    dev, _ = onboard.onboard("ssh nas")
    assert dev.name == "nas", "named after the address you gave, as the docs say"
    [e] = dev.endpoints
    assert (e["target"], e["host"], e["via"]) == ("nas", "192.168.1.20", "lan")
    [ep] = inv.endpoints_of(dev)
    assert (ep.target, ep.address, ep.via) == ("nas", "192.168.1.20", "lan")
    assert _host_of(dev) == "192.168.1.20"


def test_discover_knows_an_alias_that_was_added(home, monkeypatch):
    from fleet.ops import discover

    (home / ".ssh" / "config").write_text("Host nas\n  HostName 192.168.1.20\n")
    inv.save([Device(id="id:n", name="storage", kind=Kind.APPLIANCE,
                     endpoints=[{"target": "nas", "host": "192.168.1.20", "port": 22}])],
             inv.INVENTORY_PATH)
    [cand] = [c for c in discover.candidates(home=home, status={}) if c.name == "nas"]
    assert cand.added == "storage"


def test_is_default_identity_is_only_about_ssh_s_own_folder(home):
    assert cmd.is_default_identity(str(home / ".ssh" / "id_rsa"))
    assert not cmd.is_default_identity(str(home / "elsewhere" / "id_rsa"))
    assert not cmd.is_default_identity(str(home / ".ssh" / "vast"))
    assert not cmd.is_default_identity("")
