"""Encrypted passwords, keyed per machine.

`fleet key install` covers the common case by trading a password for key auth. A host
that genuinely refuses key auth still needs a stored credential, and a stored credential
has to be readable on every machine you use without a shared passphrase ever travelling.

Each machine holds its own age identity; secrets.age is encrypted to every enrolled
machine's public recipient. That makes removing a machine a real revocation rather than
a hope, which is the property these tests exist to pin.
"""

from __future__ import annotations

import stat

import pytest

from fleet.models import Device, Kind
from fleet.secrets import (
    SecretsError,
    ensure_identity,
    load_identity,
    read_secrets,
    recipients_of,
    write_secrets,
)


def _dev(name: str, recipient: str = "", **kw) -> Device:
    return Device(id=f"linux:machine-id:{name}", name=name,
                  kind=Kind.PERMANENT, recipient=recipient, **kw)


# --------------------------------------------------------------- identity

def test_an_identity_is_created_on_first_use(tmp_path):
    recipient = ensure_identity(tmp_path / "identity.age")
    assert recipient.startswith("age1")
    assert (tmp_path / "identity.age").exists()


def test_the_private_identity_is_not_readable_by_anyone_else(tmp_path):
    """It is the one file whose leak defeats the entire scheme."""
    path = tmp_path / "identity.age"
    ensure_identity(path)
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_asking_twice_does_not_mint_a_new_identity(tmp_path):
    """Regenerating would silently orphan every secret already encrypted to the old
    public key."""
    path = tmp_path / "identity.age"
    assert ensure_identity(path) == ensure_identity(path)


def test_recipients_are_gathered_from_devices_that_have_enrolled():
    devices = [_dev("a", "age1aaa"), _dev("b"), _dev("c", "age1ccc")]
    assert recipients_of(devices) == ["age1aaa", "age1ccc"]


# --------------------------------------------------------------- round trip

def test_a_secret_survives_encryption_and_decryption(tmp_path):
    path = tmp_path / "secrets.age"
    me = ensure_identity(tmp_path / "identity.age")
    write_secrets(path, {"blackwell": "hunter2"}, [me])
    assert read_secrets(path, load_identity(tmp_path / "identity.age")) == {"blackwell": "hunter2"}


def test_the_stored_file_does_not_contain_the_password(tmp_path):
    """The whole point. A file that syncs must be unreadable to anyone holding it."""
    path = tmp_path / "secrets.age"
    me = ensure_identity(tmp_path / "identity.age")
    write_secrets(path, {"blackwell": "hunter2"}, [me])
    assert b"hunter2" not in path.read_bytes()


def test_a_missing_secrets_file_reads_as_empty_not_as_an_error(tmp_path):
    """Most fleets have no stored passwords at all."""
    ensure_identity(tmp_path / "identity.age")
    assert read_secrets(tmp_path / "nope.age", load_identity(tmp_path / "identity.age")) == {}


# --------------------------------------------------------------- multiple machines

def test_every_enrolled_machine_can_read_the_file(tmp_path):
    """No shared passphrase travels; each machine decrypts with its own key."""
    laptop = ensure_identity(tmp_path / "laptop.age")
    desktop = ensure_identity(tmp_path / "desktop.age")
    path = tmp_path / "secrets.age"
    write_secrets(path, {"box": "pw"}, [laptop, desktop])
    for who in ("laptop.age", "desktop.age"):
        assert read_secrets(path, load_identity(tmp_path / who)) == {"box": "pw"}


def test_a_machine_that_was_never_enrolled_cannot_read_it(tmp_path):
    enrolled = ensure_identity(tmp_path / "enrolled.age")
    ensure_identity(tmp_path / "stranger.age")
    path = tmp_path / "secrets.age"
    write_secrets(path, {"box": "pw"}, [enrolled])
    with pytest.raises(SecretsError):
        read_secrets(path, load_identity(tmp_path / "stranger.age"))


def test_dropping_a_recipient_and_rewriting_is_a_real_revocation(tmp_path):
    """Revocation has to mean the removed machine genuinely cannot read the new file,
    not merely that we stopped listing it."""
    keeper = ensure_identity(tmp_path / "keeper.age")
    leaver = ensure_identity(tmp_path / "leaver.age")
    path = tmp_path / "secrets.age"
    write_secrets(path, {"box": "pw"}, [keeper, leaver])
    assert read_secrets(path, load_identity(tmp_path / "leaver.age")) == {"box": "pw"}

    write_secrets(path, {"box": "pw"}, [keeper])
    with pytest.raises(SecretsError):
        read_secrets(path, load_identity(tmp_path / "leaver.age"))


def test_writing_with_no_recipients_is_refused(tmp_path):
    """A file encrypted to nobody is unreadable to everyone, including you. Failing
    loudly beats writing a brick over a working secrets file."""
    with pytest.raises(SecretsError):
        write_secrets(tmp_path / "secrets.age", {"box": "pw"}, [])


def test_the_secrets_file_is_not_readable_by_anyone_else(tmp_path):
    path = tmp_path / "secrets.age"
    me = ensure_identity(tmp_path / "identity.age")
    write_secrets(path, {"box": "pw"}, [me])
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


# --------------------------------------------------------------- CLI

def _cli(tmp_path, monkeypatch, devices):
    from typer.testing import CliRunner

    from fleet import cli, inventory as inv, secrets, store

    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(secrets, "IDENTITY_PATH", tmp_path / "identity.age")
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "secrets.age")
    monkeypatch.setattr(cli, "local_device_id", lambda: "linux:machine-id:me")
    return CliRunner(), path


def test_identity_enrols_this_machine(tmp_path, monkeypatch):
    from fleet import inventory as inv
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch, [_dev("me")])
    result = runner.invoke(app, ["identity"])
    assert result.exit_code == 0, result.output
    assert inv.load(path)[0].recipient.startswith("age1")


def test_identity_never_prints_the_private_key(tmp_path, monkeypatch):
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch, [_dev("me")])
    result = runner.invoke(app, ["identity"])
    assert "AGE-SECRET-KEY" not in result.output


def test_identity_says_so_when_this_machine_is_not_in_the_inventory(tmp_path, monkeypatch):
    """Otherwise the recipient has nowhere to live and would never sync."""
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch, [_dev("somewhere-else")])
    monkeypatch.setattr("fleet.cli.local_device_id", lambda: "linux:machine-id:unknown")
    result = runner.invoke(app, ["identity"])
    assert result.exit_code != 0
    assert "fleet add" in result.output


def test_secret_set_refuses_without_a_terminal(tmp_path, monkeypatch):
    """Same boundary as `fleet key install`: never capture a password from a pipe."""
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch, [_dev("me")])
    runner.invoke(app, ["identity"])
    result = runner.invoke(app, ["secret", "set", "blackwell"])
    assert result.exit_code != 0
    assert "terminal" in result.output.lower() or "tty" in result.output.lower()


def test_secret_ls_shows_names_and_never_values(tmp_path, monkeypatch):
    from fleet import secrets as sec
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch, [_dev("me")])
    runner.invoke(app, ["identity"])
    me = sec.ensure_identity(tmp_path / "identity.age")
    sec.write_secrets(tmp_path / "secrets.age", {"blackwell": "hunter2"}, [me])

    result = runner.invoke(app, ["secret", "ls"])
    assert result.exit_code == 0, result.output
    assert "blackwell" in result.output
    assert "hunter2" not in result.output


def test_secret_rm_removes_only_the_named_one(tmp_path, monkeypatch):
    from fleet import secrets as sec
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch, [_dev("me")])
    runner.invoke(app, ["identity"])
    me = sec.ensure_identity(tmp_path / "identity.age")
    sec.write_secrets(tmp_path / "secrets.age", {"a": "1", "b": "2"}, [me])

    assert runner.invoke(app, ["secret", "rm", "a"]).exit_code == 0
    left = sec.read_secrets(tmp_path / "secrets.age",
                            sec.load_identity(tmp_path / "identity.age"))
    assert left == {"b": "2"}
