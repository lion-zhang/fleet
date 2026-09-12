"""Using a stored password to actually connect.

`fleet secret set` stores one; this is the half that spends it. The probe is
non-interactive and must come back as a normal ProbeResult, but its stdin is the pty
carrying the password prompt, so the payload cannot be piped in and rides in argv instead.

(The interactive `fleet ssh` half was an SSH_ASKPASS branch that could never execute --
its guard was the same condition that had already exited two statements earlier -- and it
was deleted along with its tests.)
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from fleet.probe.parse import parse_payload
from fleet.probe.runner import first_marker, password_probe_command

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


# --------------------------------------------------------------- the probe path

def test_the_payload_survives_being_shipped_as_an_argument(tmp_path):
    """stdin belongs to the password prompt, so the payload rides in argv instead.
    Running it for real is the only way to know the encoding and quoting hold."""
    cmd = password_probe_command(mode="full", disk_paths=None)
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}).stdout
    snap = parse_payload(first_marker(out))
    assert snap.os, "a real snapshot came back"


def test_configured_disk_paths_still_reach_the_payload_on_this_path(tmp_path):
    cmd = password_probe_command(mode="full", disk_paths=[str(tmp_path)])
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}).stdout
    snap = parse_payload(first_marker(out))
    assert [d.mount for d in snap.disks] == [str(tmp_path)]


def test_leading_noise_is_stripped_before_parsing():
    """A pty merges stderr into stdout, so ssh warnings and the prompt itself land in
    front of the payload output. run_probe keeps the streams apart precisely to avoid
    this; on the password path they cannot be kept apart."""
    noisy = "root@host's password: \nWarning: something\n#HOST\nos=linux\n#END rc=0\n"
    assert first_marker(noisy).startswith("#HOST")


def test_stripping_noise_leaves_clean_output_alone():
    clean = "#HOST\nos=linux\n#END rc=0\n"
    assert first_marker(clean) == clean


def test_output_with_no_marker_at_all_is_left_for_the_parser_to_reject():
    """Returning '' would look like a truncated probe; the parser should see what
    actually arrived so its error names the real problem."""
    assert first_marker("Permission denied\n") == "Permission denied\n"


# --------------------------------------------------------------- the interactive path

# --------------------------------------------------------------- CLI wiring

def _cli(tmp_path, monkeypatch, dev):
    from typer.testing import CliRunner

    from fleet import cli, inventory as inv, secrets as sec, store

    path = tmp_path / "inventory.yaml"
    inv.save([dev], path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(sec, "IDENTITY_PATH", tmp_path / "identity.age")
    monkeypatch.setattr(sec, "SECRETS_PATH", tmp_path / "secrets.age")
    monkeypatch.setattr(cli, "maybe_autosync", lambda: None)
    return CliRunner(), path


def _pw_device(**kw):
    from fleet.models import Device, Kind

    return Device(id="net:1.2.3.4:22", name="pwbox", kind=Kind.RENTAL,
                  auth_state="needs_credentials",
                  endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}], **kw)


def test_a_missing_identity_does_not_break_anything(tmp_path, monkeypatch):
    """`fleet ls` must keep working on a machine that never enrolled, has no secrets
    file, or is not a recipient. Looking up a password is best-effort by design."""
    from fleet import cli

    _cli(tmp_path, monkeypatch, _pw_device())
    assert cli.stored_password("pwbox") is None


def test_a_stored_password_is_found(tmp_path, monkeypatch):
    from fleet import cli, secrets as sec

    _cli(tmp_path, monkeypatch, _pw_device())
    me = sec.ensure_identity(tmp_path / "identity.age")
    sec.write_secrets(tmp_path / "secrets.age", {"pwbox": "hunter2"}, [me])
    assert cli.stored_password("pwbox") == "hunter2"


def test_a_host_that_rejected_our_key_is_retried_with_its_stored_password(tmp_path, monkeypatch):
    """This is the whole point of storing one: `fleet ls` should show a password-only
    host as online rather than permanently auth_failed."""
    from fleet import cli, secrets as sec, store
    from fleet.models import ProbeResult, Snapshot, Status

    _cli(tmp_path, monkeypatch, _pw_device())
    me = sec.ensure_identity(tmp_path / "identity.age")
    sec.write_secrets(tmp_path / "secrets.age", {"pwbox": "hunter2"}, [me])

    monkeypatch.setattr(cli, "probe_many", lambda jobs, **kw: {
        k: ProbeResult(status=Status.AUTH_FAILED, error_detail="key rejected")
        for k in jobs})
    used = []

    def fake_pw_probe(ep, password, **kw):
        used.append(password)
        return ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="pwbox"))

    monkeypatch.setattr(cli, "run_probe_with_password", fake_pw_probe)
    rows = cli._rows(refresh=True)
    assert used == ["hunter2"]
    assert rows[0]["status"] == "ok"


def test_a_host_with_no_stored_password_is_not_retried(tmp_path, monkeypatch):
    from fleet import cli
    from fleet.models import ProbeResult, Status

    _cli(tmp_path, monkeypatch, _pw_device())
    monkeypatch.setattr(cli, "probe_many", lambda jobs, **kw: {
        k: ProbeResult(status=Status.AUTH_FAILED, error_detail="key rejected")
        for k in jobs})
    used = []
    monkeypatch.setattr(cli, "run_probe_with_password",
                        lambda *a, **k: used.append(1))
    cli._rows(refresh=True)
    assert not used
