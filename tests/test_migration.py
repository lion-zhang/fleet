"""Spending the passwords an older fleet stored, once, and then not having them."""

from __future__ import annotations

from typer.testing import CliRunner

from fleet import cli


def _env(tmp_path, monkeypatch):
    from fleet.state import access as acl, inventory as inv, store

    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    for name in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, name, tmp_path / getattr(acl, name).name)
    return CliRunner()


def test_nothing_stored_is_not_an_error(tmp_path, monkeypatch):
    from fleet import secrets as sec

    runner = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(sec, "read_secrets", lambda *a, **k: {})
    monkeypatch.setattr(sec, "load_identity", lambda *a, **k: object())
    r = runner.invoke(cli.app, ["access", "--migrate"])
    assert r.exit_code == 0
    assert "nothing to migrate" in r.output


def test_an_unreadable_store_says_how_to_read_it(tmp_path, monkeypatch):
    """pyrage is an optional extra now, so the failure a user actually hits is "the
    reader is not installed" -- and it must say so rather than just failing."""
    from fleet import secrets as sec

    runner = _env(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("No module named 'pyrage'")

    monkeypatch.setattr(sec, "load_identity", boom)
    r = runner.invoke(cli.app, ["access", "--migrate"])
    assert r.exit_code == 2
    assert "migrate" in r.output


def test_a_failure_keeps_the_file(tmp_path, monkeypatch):
    """Install, verify, then remove -- never the reverse. A password dropped before the
    key is proven leaves a host nobody can reach, and there is no second copy."""
    from fleet.state import inventory as inv
    from fleet import secrets as sec
    from fleet.models import Device, Kind

    runner = _env(tmp_path, monkeypatch)
    inv.save([Device(id="net:1.2.3.4:22", name="box", kind=Kind.RENTAL,
                     endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])],
             inv.INVENTORY_PATH)
    monkeypatch.setattr(sec, "read_secrets", lambda *a, **k: {"box": "hunter2"})
    monkeypatch.setattr(sec, "load_identity", lambda *a, **k: object())
    monkeypatch.setattr(cli, "install_key", lambda *a, **k: (False, "Permission denied"),
                        raising=False)
    import fleet.ssh.keys as keys
    monkeypatch.setattr(keys, "install_key", lambda *a, **k: (False, "Permission denied"))
    r = runner.invoke(cli.app, ["access", "--migrate"])
    assert r.exit_code == 1
    assert "Keeping" in r.output


def test_the_wording_does_not_claim_the_bytes_are_destroyed():
    """os.replace on a journalling filesystem or an SSD does not reliably destroy the
    old blocks. Claiming otherwise would be a lie that outlives whoever wrote it."""
    import inspect

    src = inspect.getsource(cli._migrate_passwords)
    printed = [l for l in src.splitlines() if "console.print" in l or "err.print" in l]
    assert any("removed" in l for l in printed)
    assert not any("shred" in l.lower() for l in printed)
