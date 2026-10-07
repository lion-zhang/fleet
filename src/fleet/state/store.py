"""SQLite cache for telemetry. Disposable by design: delete it and the next probe
rebuilds everything. Nothing durable may live here."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..config import DB_PATH, ensure_dirs, load_config
from ..models import ProbeResult, Snapshot, Status

# The outcomes that cost a wait to learn: nothing answered, so the probe sat out the
# connect timeout. These are what back off; everything else answers at once.
SLOW_FAILURES = frozenset({Status.TIMEOUT, Status.UNREACHABLE})

# Bumped whenever the shape below changes. `CREATE TABLE IF NOT EXISTS` is silent about
# a table that exists with the wrong columns, so without this an upgraded fleet keeps the
# old schema and raises "no such column" on the first command. The telemetry tables are a
# cache, so the migration is simply to drop and refill them; `meta` is not, and survives.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_state (
  device_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'self',
  status TEXT, error_class TEXT, error_detail TEXT,
  endpoint_used TEXT, last_probe_at INTEGER, last_ok_at INTEGER, probe_ms INTEGER,
  stderr_tail TEXT, probed_by TEXT, fail_streak INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (device_id, source));
CREATE TABLE IF NOT EXISTS snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, ts INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'self', payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snapshot_dev_ts ON snapshot(device_id, source, ts DESC);
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
    # Two fleet processes opening the cache just after an upgrade used to both see the
    # old shape and both alter it; the second died on "duplicate column name". One at a
    # time, and the shape is checked again by whoever goes second.
    conn.execute("BEGIN IMMEDIATE")
    try:
        _migrate(conn)
    except BaseException:
        conn.rollback()
        raise
    if conn.in_transaction:
        conn.commit()
    conn.executescript(SCHEMA)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Drop and rebuild the telemetry tables when their shape has changed.

    Legitimate precisely because this file is a cache: the next probe refills it. `meta`
    is excluded -- `last_sync_at` living there is the one durable thing here, and losing
    it would make every upgrade trigger an immediate sweep.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION \
            and _shape_matches(conn):
        _add_columns(conn)
        return
    for table in ("device_state", "snapshot", "event"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


# Columns added after a table first shipped, with their definitions. Added in place
# rather than by bumping SCHEMA_VERSION: an additive column is the one change that does
# not need the drop-and-refill, and dropping would throw away every machine's history
# to gain a counter.
_ADDED_COLUMNS = {"device_state": {"fail_streak": "INTEGER NOT NULL DEFAULT 0"}}


def _add_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue                       # not created yet; the schema script will
        for name, ddl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


def _shape_matches(conn: sqlite3.Connection) -> bool:
    """Whether the tables really look like the current schema.

    The version alone cannot be trusted, for two reasons that compound. Databases
    written before versioning existed report 0 -- which is also what a brand-new file
    reports -- so "0 means fresh, nothing to drop" silently skipped the rebuild on every
    existing install and then stamped them as current. Those files now claim to be up to
    date while carrying the old columns, and no version check can ever fix them.

    Looking at the shape is self-healing regardless of what the stamp says, and costs one
    pragma on a file we are already opening.
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(device_state)")}
    except sqlite3.Error:
        return False
    if not cols:
        return True            # nothing created yet; executescript is about to
    return {"source", "probed_by"} <= cols


def record(conn: sqlite3.Connection, device_id: str, res: ProbeResult, *,
           source: str = "self", probed_by: str = "", at: int | None = None) -> None:
    """Store a probe result. `source` is 'self' for one we ran, 'broadcast' for one
    relayed by the center for a device we cannot reach ourselves.

    `at` is when the probe was taken, for a relayed one: stamped with the time it was
    *received*, a reading days old looked fresh, was never re-probed, and kept an agent
    sending work to a machine that had died. Never later than now.
    """
    now = int(time.time()) if at is None else min(int(at), int(time.time()))
    if not conn.in_transaction:
        # The read below and the write after it as one step: two processes probing the
        # same machine each read the streak and wrote it +1, losing a failure.
        conn.execute("BEGIN IMMEDIATE")
    prev = conn.execute("SELECT last_ok_at, fail_streak FROM device_state "
                        "WHERE device_id=? AND source=?", (device_id, source)).fetchone()
    # A failed probe degrades a device to "stale but known" -- last_ok_at is preserved
    # so the UI can say "last seen 2h ago" instead of blanking the device.
    last_ok = now if res.ok else (prev["last_ok_at"] if prev else None)
    # Only the failures that cost a wait count. A rejected key or a refused port answers
    # instantly, so re-asking is cheap -- and a key rejected a minute ago may have been
    # granted since, which is exactly when someone looks again.
    streak = ((prev["fail_streak"] or 0) if prev else 0) + 1 if res.status in SLOW_FAILURES \
        else 0
    conn.execute(
        """INSERT INTO device_state
             (device_id,source,status,error_class,error_detail,endpoint_used,
              last_probe_at,last_ok_at,probe_ms,stderr_tail,probed_by,fail_streak)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(device_id,source) DO UPDATE SET
             status=excluded.status, error_class=excluded.error_class,
             error_detail=excluded.error_detail, endpoint_used=excluded.endpoint_used,
             last_probe_at=excluded.last_probe_at, last_ok_at=excluded.last_ok_at,
             probe_ms=excluded.probe_ms, stderr_tail=excluded.stderr_tail,
             probed_by=excluded.probed_by, fail_streak=excluded.fail_streak""",
        (device_id, source, res.status.value, res.error_class, res.error_detail,
         res.endpoint_used, now, last_ok, res.latency_ms, res.stderr_tail, probed_by,
         streak))
    if res.snapshot is not None:
        conn.execute("INSERT INTO snapshot (device_id, ts, source, payload) VALUES (?,?,?,?)",
                     (device_id, res.snapshot.ts, source,
                      json.dumps(res.snapshot.to_dict(), default=str)))
        keep = int(load_config().snapshot_retention)
        # Retention is per source, or a chatty broadcast would evict the device's own
        # first-hand history from the single ring buffer they used to share.
        conn.execute(
            """DELETE FROM snapshot WHERE device_id=? AND source=? AND id NOT IN
                 (SELECT id FROM snapshot WHERE device_id=? AND source=?
                  ORDER BY ts DESC LIMIT ?)""",
            (device_id, source, device_id, source, keep))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


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


# What a member's own probe says when it simply cannot get to a machine -- not when the
# machine answered and something was wrong with it.
_CANNOT_REACH = frozenset({Status.TIMEOUT.value, Status.UNREACHABLE.value,
                           Status.AUTH_FAILED.value, Status.REFUSED.value})


def latest(conn: sqlite3.Connection, device_id: str, *,
           relayed_over_unreachable: bool = False) -> tuple[dict | None, dict | None]:
    """Return (state_row, snapshot_dict) -- either may be None.

    First-hand wins, even when the relayed row is newer. A probe we ran ourselves is
    evidence; one the center relayed is hearsay about a machine we may not be able to
    reach at all, and quietly preferring it because of a clock would make `fleet ls`
    describe someone else's view of the fleet as though it were ours.

    Except, with `relayed_over_unreachable`, when our own probe only says we could not
    get there. fleet needs the center to reach every machine, not every machine to reach
    every other: a member with no route or no key to a machine reported it down while
    the center had just measured it fine. Then the center's healthy reading, newer than
    our last success, is shown -- as relayed, with its age. For members only: on the
    center its own failure is the fact that matters, since it is the one that manages.
    """
    rows = {r["source"]: dict(r) for r in conn.execute(
        "SELECT * FROM device_state WHERE device_id=?", (device_id,)).fetchall()}
    mine, relayed = rows.get("self"), rows.get("broadcast")
    st = mine or relayed
    use = "self" if mine else "broadcast"
    if (relayed_over_unreachable and mine and relayed
            and mine["status"] in _CANNOT_REACH and relayed["status"] == Status.OK.value
            and (relayed["last_probe_at"] or 0) > (mine["last_ok_at"] or 0)):
        st, use = relayed, "broadcast"
    sn = conn.execute(
        "SELECT payload FROM snapshot WHERE device_id=? "
        "ORDER BY CASE source WHEN ? THEN 0 ELSE 1 END, ts DESC LIMIT 1",
        (device_id, use)).fetchone()
    return (st, json.loads(sn["payload"]) if sn else None)


def age_s(state: dict | None) -> int | None:
    if not state or not state.get("last_probe_at"):
        return None
    return int(time.time()) - int(state["last_probe_at"])


def is_fresh(state: dict | None, ttl: int, *, backoff_max: int = 0) -> bool:
    """Whether the last probe still stands. With `backoff_max`, a machine that has not
    answered for several probes in a row stands longer: `ttl`, doubled per failure past
    the first, up to `backoff_max`.

    Without it, a switched-off machine was dialled again on every read once a minute,
    and every read waited out the connect timeout to learn what it already knew.
    """
    a = age_s(state)
    if a is None:
        return False
    return a <= effective_ttl(state, ttl, backoff_max=backoff_max)


def effective_ttl(state: dict | None, ttl: int, *, backoff_max: int = 0) -> int:
    streak = int((state or {}).get("fail_streak") or 0)
    if not backoff_max or streak <= 1:
        return ttl
    return max(ttl, min(backoff_max, ttl * 2 ** min(streak - 1, 20)))
