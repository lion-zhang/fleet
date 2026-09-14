"""Moving the role, and refusing to when it would strand the fleet."""

from __future__ import annotations

import pathlib
import subprocess

import pytest
from typer.testing import CliRunner

from fleet.state import access as acl
from fleet import cli
from fleet.state import inventory as inv
from fleet import reconcile as rec
from fleet.state import store
from fleet.models import Device, Kind
from fleet.ops import enrol, handover


@pytest.fixture
def two_machines(tmp_path, monkeypatch):
    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")

    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    pub = key.with_suffix(".pub").read_text()
    # Patched on every module that *reads* the name, not only on the one that defines
    # it: these import it at module scope, so rebinding `fleet.ssh.keys.ensure_keypair`
    # alone leaves them holding the original and generating a real key.
    import fleet.ssh.keys
    for mod in (fleet.ssh.keys, enrol, handover):
        monkeypatch.setattr(mod, "ensure_keypair", lambda *a, **k: (key, pub),
                            raising=False)

    mine = acl.fingerprint(pub)
    other = "SHA256:otherotherotherotherotherotherotherother"
    inv.save([Device(id="id:me", name="macbook", kind=Kind.PERMANENT),
              Device(id="id:old", name="old-center", kind=Kind.PERMANENT,
                     endpoints=[{"target": "1.2.3.4", "user": "lin", "port": 22}]),
              Device(id="id:xps", name="lin-xps", kind=Kind.PERMANENT,
                     endpoints=[{"target": "5.6.7.8", "user": "lin", "port": 22}])],
             inv.INVENTORY_PATH)
    acc = acl.Access(fleet_id="7f3a9c", center=other, keys={
        other: {"name": "old-center", "pubkey": "ssh-ed25519 AAAA old", "device_id": "id:old"},
        mine: {"name": "macbook", "pubkey": pub, "device_id": "id:me"},
        "SHA256:xps": {"name": "lin-xps", "pubkey": "ssh-ed25519 AAAA x",
                       "device_id": "id:xps"},
    })
    acl.save(acc, acl.ACCESS_PATH)
    return CliRunner(), mine, other


def test_accepting_refuses_when_a_machine_cannot_be_written(two_machines, monkeypatch):
    """Probing proves a key is *present*; it says nothing about whether the file can be
    written, and on Windows every way that fails is silent. A handover verified by
    probing hands the fleet to a machine that cannot manage it -- discovered only once
    the predecessor is gone."""
    runner, mine, other = two_machines
    monkeypatch.setattr(rec, "_remote", lambda *a, **k: (False, "permission denied"))

    r = runner.invoke(cli.app, ["center", "--accept"])
    assert r.exit_code == 2
    assert "Not taking the role" in r.output
    assert acl.load(acl.ACCESS_PATH).center == other, "the old center still holds it"


def test_accepting_takes_the_role_when_every_machine_is_writable(two_machines, monkeypatch):
    runner, mine, other = two_machines
    monkeypatch.setattr(rec, "_remote", lambda *a, **k: (True, ""))

    r = runner.invoke(cli.app, ["center", "--accept"])
    assert r.exit_code == 0, r.output
    assert acl.load(acl.ACCESS_PATH).center == mine


def test_a_machine_not_in_the_list_cannot_accept(two_machines, monkeypatch):
    runner, mine, _ = two_machines
    acc = acl.load(acl.ACCESS_PATH)
    acc.keys.pop(mine)
    acl.save(acc, acl.ACCESS_PATH)

    r = runner.invoke(cli.app, ["center", "--accept"])
    assert r.exit_code == 2
    assert "not in the access list" in r.output


def test_the_handover_record_is_signed_and_names_both_ends(two_machines):
    """Spokes verify against the key they have pinned, so a new center's list is
    rejected unless something they already trust vouches for it."""
    _, mine, other = two_machines
    acc = acl.load(acl.ACCESS_PATH)
    record = acl.handover_record(acc, mine)
    assert other in record and mine in record
    assert "fleet-handover" in record


def test_nothing_promotes_a_center_except_a_handover(two_machines):
    """The load-bearing negative. Both `edit --role center` and `install --role center`
    used to call promote_center directly."""
    runner, _, _ = two_machines
    for args in (["edit", "lin-xps", "--role", "center"],
                 ["install", "lin-xps", "--role", "center"]):
        r = runner.invoke(cli.app, args)
        assert r.exit_code != 0
        assert "fleet center" in r.output


def test_no_code_path_produces_a_backup_role():
    """It meant a second machine holding a key on every device, forever."""
    import fleet

    # `fleet.__path__`, not `cli.__file__`: the package is what is being searched, and
    # deriving its directory from one module's location made this test quietly depend on
    # cli.py staying at the root. It does, but that is a packaging decision -- the
    # console script names it -- and not something a test about roles should assert.
    src = pathlib.Path(fleet.__path__[0])
    for f in src.rglob("*.py"):
        text = f.read_text()
        assert 'role = "backup"' not in text, f"{f.name} still assigns the role"
        assert 'role="backup"' not in text, f"{f.name} still assigns the role"
