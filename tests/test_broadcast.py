"""Relayed telemetry: the center measures for machines that cannot reach each other."""

from __future__ import annotations

import subprocess

import pytest

from fleet import access as acl
from fleet import cli
from fleet import store
from fleet.models import ProbeResult, Snapshot, Status


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    return tmp_path, key, key.with_suffix(".pub").read_text()


def test_telemetry_rides_the_sealed_envelope(sandbox):
    """It is signed with the inventory, not alongside it. A relayed reading decides
    where work is sent, so an unsigned one steers a job onto a machine of the sender's
    choosing."""
    _, key, pub = sandbox
    rows = [{"device_id": "id:far", "status": "ok", "probed_at": 1,
             "snapshot": {"hostname": "far"}}]
    sealed = acl.seal("devices: []\n", key_path=key, telemetry=rows)
    inventory, relayed = acl.unseal_with_telemetry(sealed, pub)
    assert inventory == "devices: []\n"
    assert relayed[0]["device_id"] == "id:far"


def test_tampering_with_the_telemetry_breaks_the_seal(sandbox):
    _, key, pub = sandbox
    sealed = acl.seal("devices: []\n", key_path=key,
                      telemetry=[{"device_id": "id:far", "status": "ok"}])
    with pytest.raises(acl.AccessError):
        acl.unseal_with_telemetry(sealed.replace("id:far", "id:evil"), pub)


def test_a_relayed_row_never_displaces_one_we_measured(sandbox):
    """Our own probe of a machine we can reach beats the center's view of it, whichever
    is newer."""
    conn = store.connect()
    store.record(conn, "id:box", ProbeResult(status=Status.OK,
                                             snapshot=Snapshot(hostname="ours")))
    conn.close()

    cli._record_relayed([{"device_id": "id:box", "status": "timeout",
                          "snapshot": {"hostname": "theirs"}}])

    conn = store.connect()
    state, snap = store.latest(conn, "id:box")
    conn.close()
    assert state["status"] == "ok" and state["source"] == "self"
    assert snap["hostname"] == "ours"


def test_a_relayed_row_is_used_where_we_have_nothing(sandbox):
    cli._record_relayed([{"device_id": "id:far", "status": "ok",
                          "snapshot": {"hostname": "far"}}])
    conn = store.connect()
    state, snap = store.latest(conn, "id:far")
    conn.close()
    assert state["source"] == "broadcast" and state["probed_by"] == "center"
    assert snap["hostname"] == "far"


def test_only_first_hand_rows_are_relayed_onward(sandbox):
    """Relaying something already relayed would let a reading drift between machines
    with nothing to say how far it had travelled."""
    conn = store.connect()
    store.record(conn, "id:mine", ProbeResult(status=Status.OK,
                                              snapshot=Snapshot(hostname="mine")))
    store.record(conn, "id:theirs", ProbeResult(status=Status.OK,
                                                snapshot=Snapshot(hostname="theirs")),
                 source="broadcast", probed_by="someone")
    conn.close()

    ids = {r["device_id"] for r in cli._telemetry_to_relay()}
    assert ids == {"id:mine"}


def test_a_malformed_row_does_not_break_the_batch(sandbox):
    """One bad entry from the far side must not cost every other machine its reading."""
    cli._record_relayed(["nonsense", {"no_device_id": True},
                         {"device_id": "id:ok", "status": "ok"}])
    conn = store.connect()
    assert store.latest(conn, "id:ok")[0] is not None
    conn.close()
