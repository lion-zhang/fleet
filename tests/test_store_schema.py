"""The cache has a shape now, and shapes change.

`CREATE TABLE IF NOT EXISTS` says nothing about a table that exists with the wrong
columns, so without a version an upgraded fleet keeps the old schema and raises on the
first command. And once the center relays telemetry for machines a spoke cannot reach,
one row per device is no longer enough to say where a number came from.
"""

from __future__ import annotations

import sqlite3

from fleet import store
from fleet.models import ProbeResult, Snapshot, Status


def _res(status=Status.OK, host="box"):
    return ProbeResult(status=status, snapshot=Snapshot(hostname=host), latency_ms=5)


def test_an_old_cache_is_rebuilt_rather_than_raising(tmp_path):
    """The v1 shape, as shipped. Opening it with the new code must not explode."""
    db = tmp_path / "cache.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE device_state (
          device_id TEXT PRIMARY KEY, status TEXT, error_class TEXT, error_detail TEXT,
          endpoint_used TEXT, last_probe_at INTEGER, last_ok_at INTEGER, probe_ms INTEGER,
          stderr_tail TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO device_state (device_id, status) VALUES ('box', 'ok');
        INSERT INTO meta (key, value) VALUES ('last_sync_at', '12345');
    """)
    old.execute("PRAGMA user_version=1")
    old.commit()
    old.close()

    conn = store.connect(db)
    store.record(conn, "box", _res())
    assert store.latest(conn, "box")[0]["status"] == "ok"
    assert store.get_meta(conn, "last_sync_at") == "12345", "meta is not a cache"
    conn.close()


def test_a_fresh_cache_is_stamped(tmp_path):
    conn = store.connect(tmp_path / "cache.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    conn.close()


def test_reopening_does_not_wipe(tmp_path):
    db = tmp_path / "cache.db"
    conn = store.connect(db)
    store.record(conn, "box", _res())
    conn.close()
    conn = store.connect(db)
    assert store.latest(conn, "box")[0] is not None
    conn.close()


def test_first_hand_beats_relayed_even_when_the_relay_is_newer(tmp_path):
    """A probe we ran is evidence; one the center relayed is hearsay about a machine we
    may not be able to reach. Preferring the newer row would let someone else's view of
    the fleet quietly replace our own."""
    conn = store.connect(tmp_path / "cache.db")
    store.record(conn, "box", _res(host="ours"))
    store.record(conn, "box", _res(status=Status.TIMEOUT, host="theirs"),
                 source="broadcast", probed_by="center")
    state, snap = store.latest(conn, "box")
    assert state["status"] == "ok"
    assert state["source"] == "self"
    assert snap["hostname"] == "ours"
    conn.close()


def test_a_relayed_row_is_used_when_we_have_none_of_our_own(tmp_path):
    conn = store.connect(tmp_path / "cache.db")
    store.record(conn, "far", _res(host="relayed"), source="broadcast", probed_by="center")
    state, snap = store.latest(conn, "far")
    assert state["source"] == "broadcast" and state["probed_by"] == "center"
    assert snap["hostname"] == "relayed"
    conn.close()


def test_a_relay_does_not_evict_our_own_history(tmp_path):
    """They used to share one ring buffer, so a chatty broadcast would push out the
    device's own snapshots."""
    conn = store.connect(tmp_path / "cache.db")
    store.record(conn, "box", _res(host="ours"))
    for _ in range(5):
        store.record(conn, "box", _res(host="theirs"), source="broadcast")
    mine = conn.execute("SELECT COUNT(*) FROM snapshot WHERE device_id='box' AND source='self'")
    assert mine.fetchone()[0] == 1
    conn.close()
