"""The write queue: many processes changing one file at once lose nothing, and a reader
never sees half a file. See src/fleet/state/writes.py."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from fleet.state import writes

N = int(os.environ.get("FLEET_TEST_WRITERS", "16"))


def _append(path: str, who: int, n: int, start) -> None:
    start.wait()
    for i in range(n):
        with writes.turn(Path(path)):
            items = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else []
            items.append(f"{who}-{i}")
            writes.atomic_write(Path(path), json.dumps(items))


def _spawn(target, args_list):
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=target, args=a) for a in args_list]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0, "a writer crashed"


def test_every_write_lands_when_many_processes_write_at_once(tmp_path):
    """The failure this replaces: load, change, save -- and the later save erased the
    earlier change. Sixteen writers, twenty-five changes each, all at the same moment."""
    path = tmp_path / "state.json"
    start = mp.get_context("spawn").Event()
    args = [(str(path), w, 25, start) for w in range(N)]
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_append, args=a) for a in args]
    for p in procs:
        p.start()
    start.set()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0, "a writer crashed"
    items = json.loads(path.read_text(encoding="utf-8"))
    assert len(items) == N * 25 and len(set(items)) == N * 25


def test_a_turn_inside_a_turn_does_not_wait_for_itself(tmp_path):
    path = tmp_path / "f"
    with writes.turn(path):
        with writes.turn(path):             # a helper that saves, called inside a turn
            writes.atomic_write(path, "x")
    assert path.read_text(encoding="utf-8") == "x"


def test_a_dead_writer_does_not_wedge_the_queue(tmp_path):
    path = tmp_path / "f"
    qdir = path.parent / f".{path.name}.queue"
    qdir.mkdir()
    # A ticket from a process that no longer exists, queued before ours.
    (qdir / f"{1:020d}-999999-00000-0000000000").write_text(
        json.dumps({"pid": 999999999, "started": 1.0}))
    t0 = time.monotonic()
    with writes.turn(path, timeout=5):
        pass
    assert time.monotonic() - t0 < 2, "the dead ticket was skipped, not waited out"
    assert not any(n for n in os.listdir(qdir) if not n.startswith("."))


def test_atomic_write_is_owner_only_and_leaves_no_temp_behind(tmp_path):
    path = tmp_path / "access.yaml"
    writes.atomic_write(path, "a: 1\n")
    writes.atomic_write(path, "a: 2\n")
    assert path.read_text(encoding="utf-8") == "a: 2\n"
    assert sorted(os.listdir(tmp_path)) == ["access.yaml"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(os.name == "nt", reason="reader/writer timing")
def test_a_reader_never_sees_a_partial_file(tmp_path):
    path = tmp_path / "big.json"
    writes.atomic_write(path, json.dumps(["x" * 100] * 2000))
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    p = ctx.Process(target=_append, args=(str(path), 0, 40, start))
    p.start()
    start.set()
    seen = 0
    while p.is_alive():
        json.loads(path.read_text(encoding="utf-8"))        # raises on a torn file
        seen += 1
    p.join()
    assert p.exitcode == 0 and seen > 0
