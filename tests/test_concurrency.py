"""Several agents on one machine change fleet's state at the same moment, and nothing is
lost: twelve simultaneous edits of one machine's record, all kept. See
docs/design/layers.md."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

N = 12


def _tag(path: str, i: int, start) -> None:
    from fleet.edit import apply_edits
    from fleet.state import inventory as inv

    start.wait()

    def add(current):
        apply_edits(inv.find_exact(current, "id:box"), add_tags=[f"t{i}"])
        return current, None

    inv.update(add, Path(path))


def _first_run(cfg: str, state: str, start, out) -> None:
    import os

    os.environ["FLEET_CONFIG_DIR"], os.environ["FLEET_STATE_DIR"] = cfg, state
    os.environ.pop("FLEET_NO_FIRST_RUN", None)
    from fleet.ops import firstrun

    firstrun.teach_agents = lambda: []          # no agents in a test
    start.wait()
    out.put(firstrun.maybe("ls", lambda line: None))


def test_simultaneous_edits_of_one_machine_all_land(tmp_path):
    from fleet.models import Device
    from fleet.state import inventory as inv

    path = tmp_path / "machines.yaml"
    inv.save([Device(id="id:box", name="box")], path)
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    procs = [ctx.Process(target=_tag, args=(str(path), i, start)) for i in range(N)]
    for p in procs:
        p.start()
    start.set()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    (box,) = inv.load(path)
    assert sorted(box.tags) == sorted(f"t{i}" for i in range(N))


def test_two_first_runs_at_once_record_this_machine_once(tmp_path):
    ctx = mp.get_context("spawn")
    start, out = ctx.Event(), ctx.Queue()
    cfg, state = tmp_path / "cfg", tmp_path / "state"
    procs = [ctx.Process(target=_first_run, args=(str(cfg), str(state), start, out))
             for _ in range(2)]
    for p in procs:
        p.start()
    start.set()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    ran = sorted(out.get(timeout=5) for _ in procs)
    assert ran == [False, True]                 # one did it, the other found it done
    from fleet.state import inventory as inv

    assert len(inv.load(cfg / "machines.yaml")) <= 1
