"""Bootstrapping key auth with a password typed once.

The password exists for exactly one purpose: to append a public key to a host's
authorized_keys. It is never stored, never logged, and never passed as an argument --
argv is world-readable in `ps`. After this runs, the host is key-auth and every other
code path in fleet works unchanged.

The pty logic is exercised against a local script that prompts the way sshd does, so
these are real tests of the interaction rather than assertions about a mock.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from fleet.keys import (
    authorized_keys_command,
    ensure_keypair,
    build_password_argv,
    public_key,
    run_with_password,
)
from fleet.sshcmd import Endpoint


def _ep(**kw) -> Endpoint:
    kw.setdefault("target", "5.6.7.8")
    kw.setdefault("user", "root")
    kw.setdefault("port", 2222)
    return Endpoint(**kw)


# --------------------------------------------------------------- the remote command

def _run_remote_command(cmd: str, home) -> None:
    """Run the generated command the way the far-side shell would."""
    subprocess.run(["sh", "-c", cmd], check=True,
                   env={"HOME": str(home), "PATH": "/usr/bin:/bin"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_the_remote_command_appends_rather_than_overwriting(tmp_path):
    """authorized_keys usually already holds keys. Truncating it would lock the user
    out of their own machine."""
    _run_remote_command(authorized_keys_command("ssh-ed25519 FIRST"), tmp_path)
    _run_remote_command(authorized_keys_command("ssh-ed25519 SECOND"), tmp_path)
    lines = (tmp_path / ".ssh" / "authorized_keys").read_text().split()
    assert "FIRST" in lines and "SECOND" in lines


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_the_remote_command_leaves_ssh_dir_unreadable_to_others(tmp_path):
    """sshd silently ignores authorized_keys when ~/.ssh is group- or world-accessible,
    which fails as a confusing 'key rejected' much later."""
    _run_remote_command(authorized_keys_command("ssh-ed25519 KEY"), tmp_path)
    assert (tmp_path / ".ssh").stat().st_mode & 0o077 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_a_hostile_key_string_cannot_execute_anything(tmp_path):
    """The key is user-supplied text placed into a remote shell command. This runs the
    real command and checks for the side effect, because asserting on the quoted string
    is exactly the kind of test that passes while the quoting is broken."""
    marker = tmp_path / "pwned"
    hostile = f"ssh-ed25519 AAAA'; touch {marker}; echo '"
    _run_remote_command(authorized_keys_command(hostile), tmp_path)
    assert not marker.exists(), "the injected command must never have run"
    assert hostile in (tmp_path / ".ssh" / "authorized_keys").read_text()


# --------------------------------------------------------------- the ssh invocation

# ------------------------------------------------------------ this machine's own key

def test_the_fleet_keypair_is_created_once(tmp_path):
    path, pub = ensure_keypair(tmp_path / "id_ed25519")
    assert path.exists() and pub.startswith("ssh-ed25519 ")
    assert "fleet:" in pub, "the comment says where an authorized_keys entry came from"


def test_the_fleet_keypair_is_never_regenerated(tmp_path):
    """A new key orphans every authorized_keys entry already placed for this machine,
    on every host, with nothing left to match them by."""
    path, first = ensure_keypair(tmp_path / "id_ed25519")
    again, second = ensure_keypair(tmp_path / "id_ed25519")
    assert again == path
    assert second == first


def test_the_private_half_is_not_readable_by_other_users(tmp_path):
    import stat

    path, _ = ensure_keypair(tmp_path / "id_ed25519")
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_a_half_written_pair_does_not_wedge_forever(tmp_path):
    """ssh-keygen refuses to overwrite, so an interrupted run would otherwise leave a
    private half that can never be completed."""
    path = tmp_path / "id_ed25519"
    path.write_text("truncated garbage from an interrupted run")
    _, pub = ensure_keypair(path)
    assert pub.startswith("ssh-ed25519 ")


def test_a_jump_host_device_can_still_be_bootstrapped():
    """build_password_argv dropped -J, so a device behind a bastion could not have a key
    installed at all. -i stays dropped: it is meaningless under PubkeyAuthentication=no.
    """
    argv = build_password_argv(Endpoint(target="box", user="root", jump="bastion",
                                        identity="/home/u/.ssh/id_ed25519"))
    assert "-J" in argv and argv[argv.index("-J") + 1] == "bastion"
    assert "-i" not in argv


def test_password_auth_is_forced_because_the_key_is_what_is_missing():
    argv = build_password_argv(_ep(), timeout=8)
    assert "PubkeyAuthentication=no" in argv
    assert "BatchMode=yes" not in argv, "BatchMode would suppress the prompt entirely"


def test_a_non_default_port_is_carried():
    assert "-p" in build_password_argv(_ep(port=2222), timeout=8)


def test_the_password_never_appears_in_argv():
    """argv is visible to every process on the machine via ps."""
    argv = build_password_argv(_ep(), timeout=8)
    assert not any("hunter2" in a for a in argv)


# --------------------------------------------------------------- the pty interaction

@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX only")
def test_a_prompting_command_is_answered_with_the_password():
    argv = ["sh", "-c", 'printf "Password: "; read p; [ "$p" = hunter2 ] && echo GRANTED']
    code, output = run_with_password(argv, "hunter2", timeout=10)
    assert code == 0
    assert "GRANTED" in output


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX only")
def test_a_wrong_password_is_reported_as_failure_not_success():
    argv = ["sh", "-c", 'printf "Password: "; read p; [ "$p" = hunter2 ] || exit 5']
    code, _ = run_with_password(argv, "wrong", timeout=10)
    assert code == 5


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX only")
def test_a_command_that_never_prompts_still_completes():
    """Key auth may already work, in which case ssh never asks anything."""
    code, output = run_with_password(["sh", "-c", "echo done"], "hunter2", timeout=10)
    assert code == 0 and "done" in output


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX only")
def test_a_hanging_command_is_killed_rather_than_waited_on_forever():
    code, _ = run_with_password(["sh", "-c", "sleep 30"], "hunter2", timeout=1)
    assert code != 0


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX only")
def test_the_password_is_not_echoed_into_the_captured_output():
    """Captured output is printed on failure and could reach a log or a transcript."""
    argv = ["sh", "-c", 'printf "Password: "; read p; echo finished']
    _, output = run_with_password(argv, "hunter2", timeout=10)
    assert "hunter2" not in output


# --------------------------------------------------------------- key discovery

def test_public_key_prefers_ed25519(tmp_path):
    (tmp_path / "id_rsa.pub").write_text("ssh-rsa AAAARSA\n")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAAED\n")
    assert "AAAAED" in public_key(tmp_path)[1]


def test_public_key_falls_back_to_whatever_exists(tmp_path):
    (tmp_path / "id_rsa.pub").write_text("ssh-rsa AAAARSA\n")
    assert "AAAARSA" in public_key(tmp_path)[1]


def test_public_key_reports_absence_rather_than_inventing_one(tmp_path):
    assert public_key(tmp_path) is None


# --------------------------------------------------------------- CLI guards

def _cli(tmp_path, monkeypatch, **devkw):
    from typer.testing import CliRunner

    from fleet import inventory as inv, store
    from fleet.models import Device, Kind

    dev = Device(id="net:5.6.7.8:2222", name="box", kind=Kind.RENTAL,
                 endpoints=[{"target": "5.6.7.8", "user": "root", "port": 2222}], **devkw)
    path = tmp_path / "inventory.yaml"
    inv.save([dev], path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    return CliRunner(), path


def test_key_install_refuses_to_prompt_without_a_terminal(tmp_path, monkeypatch):
    """An agent running this in a subprocess must get a clean error, not a hung prompt
    and not a password captured into its context."""
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch)
    result = runner.invoke(app, ["key", "install", "box"])
    assert result.exit_code != 0
    assert "terminal" in result.output.lower() or "tty" in result.output.lower()


def test_key_install_rejects_an_unknown_device(tmp_path, monkeypatch):
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch)
    assert runner.invoke(app, ["key", "install", "nosuchbox"]).exit_code != 0


def test_key_install_says_so_when_there_is_no_public_key(tmp_path, monkeypatch):
    from fleet import cli
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "public_key", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    result = runner.invoke(app, ["key", "install", "box"])
    assert result.exit_code != 0
    assert "ssh-keygen" in result.output


def test_add_does_not_prompt_for_a_password_without_a_terminal(tmp_path, monkeypatch):
    """`fleet add` is the command an agent is most likely to run unattended."""
    import fleet.cli as cli
    from fleet.cli import app
    from fleet.models import Device, Kind, ProbeResult, Status

    runner, _ = _cli(tmp_path, monkeypatch)
    dev = Device(id="net:5.6.7.8:22", name="newbox", kind=Kind.RENTAL,
                 endpoints=[{"target": "5.6.7.8", "user": "root", "port": 22}])
    monkeypatch.setattr(cli, "onboard", lambda *a, **k: (
        dev, ProbeResult(status=Status.AUTH_FAILED, error_detail="key rejected")))

    called = []
    monkeypatch.setattr(cli, "getpass", type("g", (), {
        "getpass": staticmethod(lambda *a, **k: called.append(1) or "x")})())

    result = runner.invoke(app, ["add", "ssh root@5.6.7.8"])
    assert result.exit_code == 0, result.output
    assert not called, "must never prompt when stdin is not a terminal"
    assert "fleet key install" in result.output, "but must say how to fix it"
