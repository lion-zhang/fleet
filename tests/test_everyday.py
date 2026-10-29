"""What a person using fleet day to day ran into, followed through end to end.

Install, add the machines in your ssh config, look at them in a normal-sized terminal,
change what a machine says about itself and look again, update, uninstall. Each test
is a step of that where fleet answered wrongly, or unreadably.
"""

from __future__ import annotations

import re

import pytest
from rich.console import Console
from typer.testing import CliRunner

from fleet import cli
from fleet.models import Device, Kind, ProbeResult, Status
from fleet.render.top import render_fleet, render_ls
from fleet.render.view import Detail, device_view
from fleet.state import inventory as inv
from fleet.state import store


def _snap() -> dict:
    from fleet.models import Disk, Gpu, Snapshot

    return Snapshot(
        hostname="gpubox", os="Ubuntu 24.04", cpu_cores=4,
        mem_total_kb=16_000_000, mem_avail_kb=15_000_000, gpu_present="1",
        gpus=[Gpu(i, f"GPU-{i}", "NVIDIA GeForce RTX 4090", 24564, u, 24564 - u, p)
              for i, (u, p) in enumerate([(20000, 97), (3, 0)])],
        disks=[Disk("/", 40_000_000, 9_000_000, 30_000_000, writable=True),
               Disk("/mnt/attach/project-files", 2 ** 40, 0, 2 ** 40, writable=True),
               Disk("/opt/vendor-tools", 1_000_000, 1_000_000, 0, writable=False)],
    ).to_dict()


def _rows(detail=Detail.COMPACT):
    devs = [Device(id=f"id:{n}", name=n, kind=Kind.PERMANENT, tags=["g4", "lab"],
                   endpoints=[{"target": n, "user": "me", "port": 22}])
            for n in ("gpubox", "workstation")]
    return [device_view(d, {"status": "ok", "last_probe_at": 1}, _snap(), detail)
            for d in devs]


def _lines(table, width: int) -> list[str]:
    c = Console(width=width, record=True, color_system=None)
    c.print(table)
    return c.export_text().splitlines()


# ------------------------------------------------------------------ a normal terminal
@pytest.mark.parametrize("width", [80, 100, 120])
def test_fleet_ls_stays_readable_in_a_normal_terminal(width):
    """At 100 columns every column lost an equal share: the GPU read "…" and CPU and RAM
    were gone, while a mount path stayed whole."""
    lines = _lines(render_ls(_rows(), width=width), width)
    header = lines[0]
    for col in ("NAME", "GPU", "VRAM FREE", "CPU", "RAM FREE", "DISK FREE"):
        assert col in header, (col, header)
    assert any("RTX 4090" in ln for ln in lines)
    assert all(len(ln.rstrip()) <= width for ln in lines)
    assert lines[1].lstrip().startswith("●")            # the status dot survives


@pytest.mark.parametrize("width", [100, 120])
def test_fleet_top_stays_readable_in_a_normal_terminal(width):
    rows = _rows(Detail.FULL)
    lines = _lines(render_fleet(rows, {}, width=width), width)
    for col in ("GPU", "GPU%", "VRAM FREE", "CPU", "RAM FREE"):
        assert col in lines[0], (col, lines[0])
    assert any("RTX 4090" in ln for ln in lines)


def test_an_empty_note_column_does_not_cost_the_kind():
    """With nothing to say about any machine, NOTE stayed, empty, and at 100 columns the
    KIND column went to make room for it."""
    rows = _rows()
    for r in rows:
        r["alerts"] = []
    header = _lines(render_ls(rows, width=100), 100)[0]
    assert "KIND" in header and "NOTE" not in header
    assert "NOTE" not in _lines(render_ls(rows, width=250), 250)[0]


def test_a_machine_that_did_not_answer_is_not_shown_live_in_top():
    """`fleet top dead` headed a refused machine "live", with "0G free" of memory."""
    from fleet.render.top import render_device

    dev = Device(id="id:dead", name="dead", kind=Kind.PERMANENT,
                 endpoints=[{"target": "dead", "user": "me", "port": 22}])
    row = device_view(dev, {"status": "refused", "error_class": "refused",
                            "error_detail": "nothing listening on that port",
                            "last_probe_at": 1}, None, Detail.FULL)
    text = "\n".join(_lines(render_device(row), 120))
    assert "live" not in text and "0G free" not in text
    assert "refused nothing listening on that port" in text


def test_a_wide_terminal_shows_everything():
    lines = _lines(render_ls(_rows(), width=220), 220)
    assert "/mnt/attach/project-files" in "\n".join(lines)
    for col in ("KIND", "AGE", "NOTE"):
        assert col in lines[0]


def test_read_only_mounts_are_not_listed_as_full_disks():
    """A tool volume is "0G free" for ever and can never fill: listed in `fleet ls`
    and `top`, it read as a full disk, and gave each machine two blank lines."""
    for detail, render in ((Detail.COMPACT, render_ls), (Detail.FULL, render_fleet)):
        rows = _rows(detail)
        text = "\n".join(_lines(render(rows) if render is render_ls
                                else render(rows, {}), 250))
        assert "/opt/vendor-tools" not in text
    compact = _rows()[0]
    assert [d["mount"] for d in compact["disks"]] == ["/", "/mnt/attach/project-files"]


# ------------------------------------------------------------------ the name you used
def test_the_name_a_machine_was_added_by_still_finds_it():
    """`fleet add lab` for a machine already known as `vm` adds lab as another way in;
    `fleet ssh lab` then said there was no device named lab."""
    devs = [Device(id="id:vm", name="vm", endpoints=[{"target": "lab", "user": "me"}]),
            Device(id="id:g", name="gpubox", endpoints=[{"target": "gpubox"}])]
    assert inv.find(devs, "lab").name == "vm"
    assert inv.find(devs, "gpubox").name == "gpubox"


def test_a_target_two_machines_share_finds_neither():
    devs = [Device(id="id:a", name="a", endpoints=[{"target": "10.0.0.1"}]),
            Device(id="id:b", name="b", endpoints=[{"target": "10.0.0.1"}])]
    assert inv.find(devs, "10.0.0.1") is None


# ------------------------------------------------------------------ edit, then look
def test_after_an_edit_the_next_look_reads_the_machine_again(monkeypatch):
    """`fleet edit gpubox --cost 1.5` said it was done, and `fleet ls` straight after
    showed no cost: it read the minute-old copy, from before the edit."""
    from fleet.ops import machinefile

    inv.save([Device(id="id:g", name="gpubox",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    conn = store.connect()
    try:
        store.record(conn, "id:g", ProbeResult(status=Status.OK))
    finally:
        conn.close()
    monkeypatch.setattr(machinefile, "change",
                        lambda *a, **k: ("", "cost:\n  usd_per_hour: 1.5\n"))
    r = CliRunner().invoke(cli.app, ["edit", "gpubox", "--cost", "1.5"])
    assert r.exit_code == 0, r.output
    conn = store.connect()
    try:
        state, _ = store.latest(conn, "id:g")
    finally:
        conn.close()
    assert not store.is_fresh(state, 60)


# ------------------------------------------------------------------ show
def test_show_says_a_mount_is_read_only_and_uses_the_same_units_as_ls(monkeypatch):
    inv.save([Device(id="id:g", name="gpubox", probe_policy="never",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    from fleet.models import Disk, Snapshot

    snap = Snapshot(hostname="gpubox", disks=[
        Disk("/", 40_000_000, 9_000_000, 30_000_000, writable=True),
        Disk("/data", 2 ** 40, 0, 2 ** 40, writable=True),
        Disk("/opt/vendor-tools", 1_000_000, 1_000_000, 0, writable=False)])
    conn = store.connect()
    try:
        store.record(conn, "id:g", ProbeResult(status=Status.OK, snapshot=snap))
    finally:
        conn.close()
    r = CliRunner().invoke(cli.app, ["show", "gpubox", "--no-refresh"])
    assert r.exit_code == 0, r.output
    vendor = next(ln for ln in r.output.splitlines() if "/opt/vendor-tools" in ln)
    assert "read-only" in vendor
    data = next(ln for ln in r.output.splitlines() if "/data" in ln)
    assert re.search(r"\b1\.0P free", data), data


# ------------------------------------------------------------------ update
def test_update_says_which_version_a_machine_is_on(monkeypatch):
    inv.save([Device(id="id:g", name="gpubox",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    monkeypatch.setattr(cli, "run_installer",
                        lambda ep, script, *, forward_agent, platform: (0, "fleet 0.6.1\n"))
    r = CliRunner().invoke(cli.app, ["update", "gpubox"])
    assert r.exit_code == 0, r.output
    assert "fleet 0.6.1" in r.output


# ------------------------------------------------------------------ uninstall
def test_purge_takes_the_probe_s_cache_on_this_machine_too(tmp_path, monkeypatch):
    """fleet reads the machine it runs on as it reads any other, so the probe's cache
    is here too -- and --purge, which deletes fleet's files, left it."""
    from fleet.ops import uninstall

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(uninstall.Path, "home", staticmethod(lambda: tmp_path / "home"))
    cache = tmp_path / "cache" / "fleet"
    (cache / "du.lock").mkdir(parents=True)
    (cache / "software").write_text("#v1 1 x\n")
    (cache / "sizes").write_text("#v1 1 x\n")
    for machine in ("7f3c", "node-2"):       # one folder for each machine in a shared home
        (cache / machine / "du.lock").mkdir(parents=True)
        (cache / machine / "software").write_text("#v1 1 x\n")
        (cache / machine / "sizes").write_text("#v1 1 x\n")
    uninstall.uninstall(purge=True)
    assert not cache.exists()
