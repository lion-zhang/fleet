"""SQLite cache for telemetry. Disposable by design: delete it and the next probe
rebuilds everything. Nothing durable may live here."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from ..config import DB_PATH, ensure_dirs, load_config
from ..models import ProbeResult, Snapshot, Status
from .timing import since

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
    path = path or DB_PATH
    conn = None
    try:
        ensure_dirs()
        # SQLite quietly opens a file it may not write read-only, and the first write
        # then fails far from here; ask first.
        for p in (path, path.parent):
            if p.exists() and not os.access(p, os.W_OK):
                raise PermissionError(f"{p} is not writable")
        conn = sqlite3.connect(str(path), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return _prepare(conn)
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError):
                raise
            # "file is not a database", "database disk image is malformed": a cache
            # half-written by a crash or a full disk stopped every command. It is
            # disposable by design, so it is set aside and started again.
            conn.close()
            conn = None
            _set_aside(path, exc)
            conn = sqlite3.connect(str(path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL")
            return _prepare(conn)
    except (OSError, sqlite3.OperationalError) as exc:
        # A sandbox that lets fleet read but not write its state dir -- Codex's
        # workspace-write, running `tar ... && fleet ssh ...` -- crashed every command
        # with "unable to open database file". The file is a cache: read what is there,
        # keep this run's writes in memory, and say once that they will not be kept.
        if conn is not None:
            conn.close()
        return _in_memory(path, exc)


def _set_aside(path: Path, exc: BaseException) -> None:
    aside = path.with_name(f"{path.name}.broken-{int(time.time())}")
    for suffix in ("", "-wal", "-shm"):
        part = Path(f"{path}{suffix}")
        if part.exists():
            os.replace(part, Path(f"{aside}{suffix}"))
    print(f"fleet: the cache {path} could not be read ({exc}); moved it to {aside.name} "
          "and started a new one", file=sys.stderr)


_warned = False


def _in_memory(path: Path, exc: BaseException) -> sqlite3.Connection:
    global _warned
    if not _warned:
        _warned = True
        print(f"fleet: cannot write its cache at {path} ({exc}); carrying on without "
              "saving what this run sees", file=sys.stderr)
    conn = sqlite3.connect(":memory:")
    try:
        disk = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            disk.backup(conn)
        finally:
            disk.close()
    except (OSError, sqlite3.Error):
        pass                       # nothing readable there either: start empty
    return _prepare(conn)


def _prepare(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # Two fleet processes opening the cache just after an upgrade used to both see the
    # old shape and both alter it; the second died on "duplicate column name". One at a
    # time, and the shape is checked again by whoever goes second.
    #
    # Only when there is something to migrate: taken on every open, every `fleet ls`
    # queued behind whichever process was writing, which is the one thing a read must
    # never do.
    if _needs_migration(conn):
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


def _needs_migration(conn: sqlite3.Connection) -> bool:
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return True
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"device_state", "snapshot", "meta"} <= tables or not _shape_matches(conn):
            return True
        for table, columns in _ADDED_COLUMNS.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if set(columns) - have:
                return True
    except sqlite3.Error:
        return True
    return False


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
           source: str = "self", probed_by: str = "", at: int | None = None,
           only_if_newer: bool = False) -> bool:
    """Store a probe result. Returns whether anything was written.

    `source` and `probed_by` are columns from when a reading could also be relayed by
    another machine; every reading is now our own (`self`). `at` is when the probe was
    taken, never later than now. `only_if_newer` drops a reading no newer than the one
    stored.
    """
    now = int(time.time()) if at is None else min(int(at), int(time.time()))
    started = not conn.in_transaction
    if started:
        # The read below and the write after it as one step: two processes probing the
        # same machine each read the streak and wrote it +1, losing a failure.
        conn.execute("BEGIN IMMEDIATE")
    prev = conn.execute("SELECT last_ok_at, fail_streak, last_probe_at FROM device_state "
                        "WHERE device_id=? AND source=?", (device_id, source)).fetchone()
    if only_if_newer and prev and int(prev["last_probe_at"] or 0) >= now:
        if started:
            conn.rollback()
        return False
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
        payload = res.snapshot.to_dict()
        if payload.get("light"):
            last = conn.execute("SELECT payload FROM snapshot WHERE device_id=? AND source=? "
                                "ORDER BY id DESC LIMIT 1", (device_id, source)).fetchone()
            if last:
                carry_over(json.loads(last["payload"]), payload)
        conn.execute("INSERT INTO snapshot (device_id, ts, source, payload) VALUES (?,?,?,?)",
                     (device_id, res.snapshot.ts, source, json.dumps(payload, default=str)))
        keep = int(load_config().snapshot_retention)
        conn.execute(
            """DELETE FROM snapshot WHERE device_id=? AND source=? AND id NOT IN
                 (SELECT id FROM snapshot WHERE device_id=? AND source=?
                  ORDER BY id DESC LIMIT ?)""",
            (device_id, source, device_id, source, keep))
    conn.commit()
    return True


def carry_over(prev: dict, snap: dict) -> None:
    """Fill what a light probe skipped from the reading before it.

    `fleet top` probes light -- no HTTP calls, no docker -- and every frame is stored, so
    without this a `fleet ls` right after `top` would find the models every server was
    serving, and the running containers, gone.
    """
    if snap.get("containers") is None and prev.get("containers") is not None:
        snap["containers"] = prev["containers"]
    before = {s.get("port"): s for s in prev.get("services") or [] if s.get("status")}
    for svc in snap.get("services") or []:
        if not svc.get("status") and (old := before.get(svc.get("port"))) \
                and old.get("comm") == svc.get("comm"):
            for k in ("status", "healthy", "latency_ms", "model_ids", "kind"):
                svc[k] = old.get(k)


def forget_except(conn: sqlite3.Connection, keep: set[str]) -> int:
    """Drop every cached reading of a machine not in `keep`. Returns how many machines.

    Rows of machines removed from the fleet, or re-identified, were kept for good, and
    `telemetry_to_relay` went on sending them in every message.
    """
    if not keep:
        return 0                               # an empty inventory is a reason to stop
    gone = [r[0] for r in conn.execute("SELECT DISTINCT device_id FROM device_state")
            if r[0] not in keep]
    for device_id in gone:
        conn.execute("DELETE FROM device_state WHERE device_id=?", (device_id,))
        conn.execute("DELETE FROM snapshot WHERE device_id=?", (device_id,))
    if gone:
        conn.commit()
    return len(gone)


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


def expire(conn: sqlite3.Connection, device_id: str) -> None:
    """Make the next read probe this machine again rather than trust what is stored.

    For a change made on the machine itself -- its machine.yaml, by `fleet edit` --
    which the reading cached a minute ago knows nothing of: `fleet ls` straight after
    an edit showed the old cost and the old alert.
    """
    with conn:
        conn.execute("UPDATE device_state SET last_probe_at=0 WHERE device_id=?",
                     (device_id,))


def latest(conn: sqlite3.Connection, device_id: str) -> tuple[dict | None, dict | None]:
    """Return (state_row, snapshot_dict) -- either may be None."""
    st = conn.execute("SELECT * FROM device_state WHERE device_id=? AND source='self'",
                      (device_id,)).fetchone()
    sn = conn.execute(
        "SELECT payload FROM snapshot WHERE device_id=? AND source='self' "
        # By arrival, not by `ts`: after the clock jumps back, the newest reading has the
        # smallest timestamp, and sorting by it showed a stale one forever.
        "ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
    return (dict(st) if st else None, json.loads(sn["payload"]) if sn else None)


def age_s(state: dict | None) -> int | None:
    """Seconds since the last probe, or None -- also when the clock has gone back since,
    which printed an AGE of -7200s and, in `top`, called the reading live."""
    if not state or not state.get("last_probe_at"):
        return None
    elapsed = since(int(state["last_probe_at"]))
    return None if elapsed is None else int(elapsed)


def is_fresh(state: dict | None, ttl: int, *, backoff_max: int = 0) -> bool:
    """Whether the last probe still stands. With `backoff_max`, a machine that has not
    answered for several probes in a row stands longer: `ttl`, doubled per failure past
    the first, up to `backoff_max`.

    Without it, a switched-off machine was dialled again on every read once a minute,
    and every read waited out the connect timeout to learn what it already knew.
    """
    a = age_s(state)
    if a is None or a < 0:
        return False                       # from the future: the clock went back
    return a <= effective_ttl(state, ttl, backoff_max=backoff_max)


def effective_ttl(state: dict | None, ttl: int, *, backoff_max: int = 0) -> int:
    from .timing import backoff

    return int(backoff(ttl, int((state or {}).get("fail_streak") or 0), backoff_max))
