"""The cache has a shape now, and shapes change.

`CREATE TABLE IF NOT EXISTS` says nothing about a table that exists with the wrong
columns, so without a version an upgraded fleet keeps the old schema and raises on the
first command. And once the center relays telemetry for machines a spoke cannot reach,
one row per device is no longer enough to say where a number came from.
"""

from __future__ import annotations

import sqlite3

import pytest

from fleet.state import store
from fleet.models import ProbeResult, Snapshot, Status


def _res(status=Status.OK, host="box"):
    return ProbeResult(status=status, snapshot=Snapshot(hostname=host), latency_ms=5)


V1 = """
    CREATE TABLE device_state (
      device_id TEXT PRIMARY KEY, status TEXT, error_class TEXT, error_detail TEXT,
      endpoint_used TEXT, last_probe_at INTEGER, last_ok_at INTEGER, probe_ms INTEGER,
      stderr_tail TEXT);
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO device_state (device_id, status) VALUES ('box', 'ok');
    INSERT INTO meta (key, value) VALUES ('last_sync_at', '12345');
"""


def _v1_cache(db, *, stamped: int):
    old = sqlite3.connect(db)
    old.executescript(V1)
    old.execute(f"PRAGMA user_version={stamped}")
    old.commit()
    old.close()


@pytest.mark.parametrize("stamped", [0, 1, 2])
def test_an_old_cache_is_rebuilt_rather_than_raising(tmp_path, stamped):
    """Three stamps, three ways this went wrong.

    0 is what every database written before versioning existed reports -- and also what
    a brand-new file reports, which is why "0 means fresh" skipped the rebuild on every
    real install. 2 is the state those files were then left in: stamped current while
    carrying the old columns, so no version check could ever repair them. 1 is the only
    one the original test covered, and it never existed anywhere.
    """
    db = tmp_path / "cache.db"
    _v1_cache(db, stamped=stamped)

    conn = store.connect(db)
    store.record(conn, "box", _res())
    assert store.latest(conn, "box")[0]["status"] == "ok"
    assert store.get_meta(conn, "last_sync_at") == "12345", "meta is not a cache"
    conn.close()


def test_a_brand_new_cache_is_not_mistaken_for_an_old_one(tmp_path):
    """The other half of the ambiguity: version 0 with no tables is simply new."""
    conn = store.connect(tmp_path / "cache.db")
    store.record(conn, "box", _res())
    assert store.latest(conn, "box")[0]["status"] == "ok"
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


def test_a_cache_fleet_cannot_write_is_read_and_not_crashed_on(tmp_path, monkeypatch, capsys):
    """Codex's workspace-write sandbox lets fleet read its state dir but not write it,
    and `tar ... && fleet ssh box` died with "OperationalError: unable to open database
    file". The cache is disposable: read what is there, keep nothing, say so once."""
    import os

    from fleet.models import ProbeResult, Snapshot, Status
    from fleet.state import store

    db = tmp_path / "cache.db"
    conn = store.connect(db)
    store.record(conn, "id:box", ProbeResult(status=Status.OK,
                                             snapshot=Snapshot(ts=1, hostname="box")))
    conn.close()
    monkeypatch.setattr(store, "_warned", False)
    real = os.access
    monkeypatch.setattr(os, "access", lambda p, mode, **k: False if mode == os.W_OK
                        else real(p, mode, **k))
    for _ in range(2):
        conn = store.connect(db)
        assert store.latest(conn, "id:box")[1]["hostname"] == "box", "what is there is read"
        store.record(conn, "id:new", ProbeResult(status=Status.TIMEOUT))
        conn.close()
    assert capsys.readouterr().err.count("cannot write its cache") == 1
    monkeypatch.setattr(os, "access", real)
    conn = store.connect(db)
    assert store.latest(conn, "id:new") == (None, None), "and nothing is written"
    conn.close()


def test_a_state_dir_that_cannot_exist_still_lets_fleet_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fleet import cli, config
    from fleet.models import Device, Kind
    from fleet.state import inventory as inv
    from fleet.state import store

    inv.save([Device(id="id:phone", name="phone", kind=Kind.MOBILE, probe_policy="never")])
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    monkeypatch.setattr(store, "DB_PATH", blocked / "state" / "cache.db")
    monkeypatch.setattr(config, "STATE_DIR", blocked / "state")
    monkeypatch.setattr(store, "_warned", False)
    for args in (["ls"], ["ls", "--json"], ["show", "phone"], ["doctor"]):
        r = CliRunner().invoke(cli.app, args)
        assert r.exception is None or isinstance(r.exception, SystemExit), (args, r.output)


@pytest.mark.parametrize("junk", [b"this is not sqlite at all" * 200,
                                  b"SQLite format 3\x00" + b"\x00" * 90])
def test_a_cache_that_cannot_be_read_is_set_aside_and_started_again(tmp_path, junk, capsys):
    """A cache half-written by a crash or a full disk stopped every command with
    "file is not a database". It is disposable by design."""
    db = tmp_path / "cache.db"
    db.write_bytes(junk)
    conn = store.connect(db)
    try:
        store.record(conn, "box", _res())
        assert store.latest(conn, "box")[1] is not None
    finally:
        conn.close()
    assert list(tmp_path.glob("cache.db.broken-*"))
    assert "started a new one" in capsys.readouterr().err


def test_a_reading_of_nan_is_no_reading():
    from fleet.probe.parse import _float

    assert _float("nan") is None and _float("inf") is None and _float("1.5") == 1.5
