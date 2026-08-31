"""SQLite cache for telemetry. Disposable by design: delete it and the next probe
rebuilds everything. Nothing durable may live here."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import DB_PATH, ensure_dirs, load_config
from .models import ProbeResult, Snapshot, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_state (
  device_id TEXT PRIMARY KEY, status TEXT, error_class TEXT, error_detail TEXT,
  endpoint_used TEXT, last_probe_at INTEGER, last_ok_at INTEGER, probe_ms INTEGER,
  stderr_tail TEXT);
CREATE TABLE IF NOT EXISTS snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, ts INTEGER NOT NULL,
  payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snapshot_dev_ts ON snapshot(device_id, ts DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, device_id TEXT, kind TEXT, detail TEXT);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(path or DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def record(conn: sqlite3.Connection, device_id: str, res: ProbeResult) -> None:
    now = int(time.time())
    prev = conn.execute("SELECT last_ok_at FROM device_state WHERE device_id=?",
                        (device_id,)).fetchone()
    # A failed probe degrades a device to "stale but known" -- last_ok_at is preserved
    # so the UI can say "last seen 2h ago" instead of blanking the device.
    last_ok = now if res.ok else (prev["last_ok_at"] if prev else None)
    conn.execute(
        """INSERT INTO device_state
             (device_id,status,error_class,error_detail,endpoint_used,last_probe_at,last_ok_at,probe_ms,stderr_tail)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET
             status=excluded.status, error_class=excluded.error_class,
             error_detail=excluded.error_detail, endpoint_used=excluded.endpoint_used,
             last_probe_at=excluded.last_probe_at, last_ok_at=excluded.last_ok_at,
             probe_ms=excluded.probe_ms, stderr_tail=excluded.stderr_tail""",
        (device_id, res.status.value, res.error_class, res.error_detail,
         res.endpoint_used, now, last_ok, res.latency_ms, res.stderr_tail))
    if res.snapshot is not None:
        conn.execute("INSERT INTO snapshot (device_id, ts, payload) VALUES (?,?,?)",
                     (device_id, res.snapshot.ts, json.dumps(res.snapshot.to_dict(), default=str)))
        keep = int(load_config().snapshot_retention)
        conn.execute(
            """DELETE FROM snapshot WHERE device_id=? AND id NOT IN
                 (SELECT id FROM snapshot WHERE device_id=? ORDER BY ts DESC LIMIT ?)""",
            (device_id, device_id, keep))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key,value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def rename_device(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Carry a device's cached rows to a new identity.

    Needed when a never-probed device is re-addressed: `net:<host>:<port>` was its id,
    so a new address means a new id, and history keyed by the old one would be orphaned.
    If the new identity already has rows they are the truthful ones and win -- the
    device_state primary key would reject the update anyway.
    """
    if old_id == new_id:
        return
    conn.execute("DELETE FROM device_state WHERE device_id=? AND EXISTS "
                 "(SELECT 1 FROM device_state WHERE device_id=?)", (old_id, new_id))
    for table in ("device_state", "snapshot", "event"):
        conn.execute(f"UPDATE OR REPLACE {table} SET device_id=? WHERE device_id=?",
                     (new_id, old_id))
    conn.commit()


def latest(conn: sqlite3.Connection, device_id: str) -> tuple[dict | None, dict | None]:
    """Return (state_row, snapshot_dict) -- either may be None."""
    st = conn.execute("SELECT * FROM device_state WHERE device_id=?", (device_id,)).fetchone()
    sn = conn.execute("SELECT payload FROM snapshot WHERE device_id=? ORDER BY ts DESC LIMIT 1",
                      (device_id,)).fetchone()
    return (dict(st) if st else None, json.loads(sn["payload"]) if sn else None)


def age_s(state: dict | None) -> int | None:
    if not state or not state.get("last_probe_at"):
        return None
    return int(time.time()) - int(state["last_probe_at"])


def is_fresh(state: dict | None, ttl: int) -> bool:
    a = age_s(state)
    return a is not None and a <= ttl
