"""A member with a wrong clock, and what a member's readings may say.

Found in review: a member whose clock ran ahead stamped its edits in the future, so its
own copy preferred them over the center's changes until the clock caught up; readings
relayed between machines carried the sender's clock; and the center took a member's
readings of any machine, including ones it measures itself -- a way to make a busy GPU
look free to every agent on the center.
"""

from __future__ import annotations

import time

from fleet.models import Device, Kind, ProbeResult, Snapshot, Status
from fleet.ops import sync
from fleet.state import access as acl
from fleet.state import clock, store
from fleet.state import inventory as inv


def test_the_center_says_when_it_answered(tmp_path):
    from fleet import config
    import subprocess

    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f",
                    str(config.FLEET_KEY)], check=True)
    pub = config.FLEET_KEY.with_suffix(".pub").read_text()
    before = int(time.time())
    note = acl.unseal(acl.seal("devices: []\n"), pub)
    assert before <= note["sent_at"] <= int(time.time())


def test_a_member_learns_how_far_its_clock_is_from_the_centers():
    clock.note_center_time(int(time.time()) + 3600)
    assert 3598 <= clock.offset() <= 3600
    assert abs(clock.now() - (int(time.time()) + 3600)) <= 2
    clock.note_center_time(int(time.time()) + 1)      # latency, not a wrong clock
    assert clock.offset() == 0
    clock.note_center_time(0)                         # an old center says nothing
    assert clock.offset() == 0


def test_the_center_never_corrects_its_own_clock():
    clock.note_center_time(int(time.time()) + 3600)   # left from being a member
    acl.ACCESS_PATH.write_text("fleet_id: x\n")
    assert clock.offset() == 0


def test_changes_are_stamped_by_the_centers_clock():
    clock.note_center_time(int(time.time()) - 7200)   # this clock is 2h ahead
    d = Device(id="x", name="x", kind=Kind.PERMANENT)
    inv.touch(d)
    assert abs(d.updated_at - (int(time.time()) - 7200)) <= 2


def test_a_record_from_a_fast_clock_stops_hiding_the_centers_changes():
    now = int(time.time())
    mine = [Device(id="x", name="old-name", kind=Kind.PERMANENT, updated_at=now + 3600)]
    same = [Device(id="x", name="old-name", kind=Kind.PERMANENT, updated_at=now)]
    merged, _ = inv.merge_from_center(mine, same, sent_at=now)
    assert merged[0].updated_at == now, "capped at the moment the center answered"
    renamed = [Device(id="x", name="new-name", kind=Kind.PERMANENT, updated_at=now + 60)]
    merged, _ = inv.merge_from_center(merged, renamed, sent_at=now + 61)
    assert merged[0].name == "new-name", "the center's later change now wins"


def test_relayed_times_are_read_in_this_machines_clock():
    clock.note_center_time(int(time.time()) + 600)    # the center is 10 min ahead
    center_time = int(time.time()) + 600 - 30         # it measured 30 s ago
    sync.record_relayed([{"device_id": "gpu", "status": "ok", "probed_at": center_time,
                          "snapshot": {"hostname": "gpu"}}])
    conn = store.connect()
    row, _ = store.latest(conn, "gpu")
    conn.close()
    assert abs(row["last_probe_at"] - (int(time.time()) - 30)) <= 3


def test_what_a_member_sends_is_in_the_centers_clock():
    clock.note_center_time(int(time.time()) + 600)
    conn = store.connect()
    store.record(conn, "me", ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="me")))
    conn.close()
    sent = sync.telemetry_to_relay()
    assert abs(sent[0]["probed_at"] - (int(time.time()) + 600)) <= 3


def test_the_center_takes_a_members_word_only_where_it_has_none_of_its_own():
    conn = store.connect()
    store.record(conn, "gpu", ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="gpu")))
    conn.close()
    rows = [{"device_id": d, "status": "ok", "probed_at": int(time.time()),
             "snapshot": {"hostname": d}} for d in ("gpu", "member", "far")]
    sync.record_relayed(rows, sender_id="member", by="laptop")
    conn = store.connect()
    try:
        srcs = {r["device_id"]: r["probed_by"] for r in conn.execute(
            "SELECT device_id, probed_by FROM device_state WHERE source='broadcast'")}
    finally:
        conn.close()
    assert srcs == {"member": "laptop", "far": "laptop"}, srcs
