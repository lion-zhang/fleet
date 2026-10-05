"""A machine that is off costs freshness, not time.

Measured before this existed: one switched-off machine made `fleet ls` wait out the
connect timeout once a minute, and a member whose center was closed waited it out on
every read, forever. Both are the outage the design says must cost freshness only.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from fleet.models import Device, Kind, ProbeResult, Status
from fleet.state import access as acl
from fleet.state import inventory as inv
from fleet.state import store


def _set_age(conn, device_id, seconds):
    conn.execute("UPDATE device_state SET last_probe_at=? WHERE device_id=?",
                 (int(time.time()) - seconds, device_id))
    conn.commit()


def _fail(conn, device_id, times, status=Status.TIMEOUT):
    for _ in range(times):
        store.record(conn, device_id, ProbeResult(status=status))


# ------------------------------------------------------------------- the streak

def test_only_failures_that_cost_a_wait_count():
    conn = store.connect()
    _fail(conn, "id:a", 3)
    assert store.latest(conn, "id:a")[0]["fail_streak"] == 3
    _fail(conn, "id:a", 1, Status.UNREACHABLE)
    assert store.latest(conn, "id:a")[0]["fail_streak"] == 4
    # A rejected key answers at once, and may have been granted since: it resets.
    _fail(conn, "id:a", 1, Status.AUTH_FAILED)
    assert store.latest(conn, "id:a")[0]["fail_streak"] == 0


def test_an_answer_ends_the_streak():
    conn = store.connect()
    _fail(conn, "id:a", 5)
    store.record(conn, "id:a", ProbeResult(status=Status.OK))
    assert store.latest(conn, "id:a")[0]["fail_streak"] == 0


@pytest.mark.parametrize("streak,expected", [(0, 60), (1, 60), (2, 120), (3, 240),
                                             (6, 1800), (50, 1800)])
def test_the_wait_doubles_and_stops(streak, expected):
    assert store.effective_ttl({"fail_streak": streak}, 60, backoff_max=1800) == expected


def test_without_a_ceiling_nothing_backs_off():
    assert store.effective_ttl({"fail_streak": 9}, 60, backoff_max=0) == 60


def test_an_existing_cache_gains_the_column_and_keeps_its_history(tmp_path):
    """Additive, so no drop-and-refill: that would discard every machine's history."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
      CREATE TABLE device_state (device_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'self',
        status TEXT, error_class TEXT, error_detail TEXT, endpoint_used TEXT,
        last_probe_at INTEGER, last_ok_at INTEGER, probe_ms INTEGER, stderr_tail TEXT,
        probed_by TEXT, PRIMARY KEY (device_id, source));
      INSERT INTO device_state (device_id, status, last_ok_at) VALUES ('id:a', 'ok', 123);
    """)
    old.execute(f"PRAGMA user_version={store.SCHEMA_VERSION}")
    old.commit()
    old.close()
    conn = store.connect(path)
    row = store.latest(conn, "id:a")[0]
    assert row["last_ok_at"] == 123 and row["fail_streak"] == 0


# ------------------------------------------------------------------- fleet ls

@pytest.fixture
def one_dead_machine(monkeypatch):
    from fleet.ops import rows

    inv.save([Device(id="id:dead", name="dead", kind=Kind.PERMANENT,
                     endpoints=[{"name": "primary", "target": "dead.example",
                                 "user": "root", "port": 22}])])
    conn = store.connect()
    _fail(conn, "id:dead", 3)                 # trusted for 240s at the default ttl
    _set_age(conn, "id:dead", 100)
    conn.close()
    dialled = []
    monkeypatch.setattr(rows, "_probe", lambda conn, stale, cfg, **k:
                        dialled.extend(d.name for d in stale))
    monkeypatch.setattr(rows, "_render", lambda *a, **k: [])
    return rows, dialled


def test_a_bare_read_leaves_a_machine_that_keeps_not_answering_alone(one_dead_machine):
    rows, dialled = one_dead_machine
    rows.snapshot()
    assert dialled == []


def test_naming_the_machine_asks_it(one_dead_machine):
    rows, dialled = one_dead_machine
    rows.snapshot(["dead"])
    assert dialled == ["dead"]


def test_refresh_asks_it(one_dead_machine):
    rows, dialled = one_dead_machine
    rows.snapshot(refresh=True)
    assert dialled == ["dead"]


def test_once_the_wait_is_over_it_is_asked_again(one_dead_machine):
    rows, dialled = one_dead_machine
    conn = store.connect()
    _set_age(conn, "id:dead", 300)
    conn.close()
    rows.snapshot()
    assert dialled == ["dead"]


# ------------------------------------------------------------------- the center

@pytest.fixture
def a_member(monkeypatch):
    from fleet.ops import sync

    acl.pin_center_pubkey("ssh-ed25519 AAAA center")
    acl.note_center_url("http://hub.example/sync")
    monkeypatch.setattr(acl, "seal", lambda *a, **k: "sealed")
    asked = []
    monkeypatch.setattr(sync, "post", lambda url, payload, **k: asked.append(url) or None)
    return sync, asked


def test_a_center_that_did_not_answer_is_not_asked_on_every_read(a_member):
    sync, asked = a_member
    sync.ensure_fresh()
    sync.ensure_fresh()
    sync.ensure_fresh()
    assert len(asked) == 1


def test_it_is_asked_again_once_the_wait_is_over(a_member, monkeypatch):
    sync, asked = a_member
    sync.ensure_fresh()
    later = time.time() + 61
    monkeypatch.setattr(time, "time", lambda: later)
    sync.ensure_fresh()
    assert len(asked) == 2


def test_forcing_asks_regardless(a_member):
    sync, asked = a_member
    sync.ensure_fresh()
    sync.ensure_fresh(force=True)
    assert len(asked) == 2


def test_the_wait_doubles_per_miss_up_to_the_ceiling():
    for _ in range(3):
        acl.note_center_unanswered()
    assert 230 <= acl.center_retry_after(60, 1800) <= 240
    for _ in range(10):
        acl.note_center_unanswered()
    assert 1790 <= acl.center_retry_after(60, 1800) <= 1800


def test_hearing_from_the_center_ends_the_wait():
    acl.note_center_unanswered()
    assert acl.center_retry_after(60, 1800) > 0
    acl.note_center_seen()
    assert acl.center_retry_after(60, 1800) == 0


def test_a_reply_from_the_wrong_key_is_backed_off_too(a_member, monkeypatch):
    sync, asked = a_member
    monkeypatch.setattr(sync, "post", lambda url, payload, **k: asked.append(url) or "junk")
    sync.ensure_fresh()
    sync.ensure_fresh()
    assert len(asked) == 1
