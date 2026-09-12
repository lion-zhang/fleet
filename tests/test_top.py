"""The live view.

Everything that decides *what* is shown and *when* it is refreshed lives in pure
functions here, so the interesting logic is testable without a terminal. The Live loop
itself is a thin shell around them.

The scheduling half matters more than the rendering half: a live view that polls every
device every two seconds would hammer a shared cluster the codebase explicitly promises
not to hammer, and would redial a dead host hundreds of times an hour.
"""

from __future__ import annotations

from fleet.models import Device, Kind
from fleet.top import (EMPTY, FILLED, Schedule, cpu_pct, device_lines, disk_cell,
                       gpu_cells, gpu_cells_compact, gpu_pct, meter, name_cell,
                       render_fleet, staleness)


def _dev(name: str, kind: Kind = Kind.PERMANENT) -> Device:
    return Device(id=f"linux:machine-id:{name}", name=name, kind=kind)


# --------------------------------------------------------------- meters

def test_a_meter_fills_in_proportion():
    assert meter(50, 100, width=10) == "[█████░░░░░]"


def test_a_full_meter_is_full():
    assert meter(100, 100, width=4) == "[████]"


def test_an_empty_meter_is_empty():
    assert meter(0, 100, width=4) == "[░░░░]"


def test_a_meter_with_no_capacity_does_not_divide_by_zero():
    """A device with no GPU reports totals of zero, and a crash in the render loop
    takes down the whole view."""
    assert meter(0, 0, width=4) == "[░░░░]"


def test_a_meter_cannot_overflow_its_width():
    """Reported usage can exceed the total on some drivers; the row must not shift."""
    assert len(meter(150, 100, width=6)) == len(meter(50, 100, width=6))


def test_a_meter_is_not_swallowed_by_rich_markup():
    """rich reads "[#..." inside brackets as a hex colour tag and eats the whole bar.
    Rendering a meter must survive being handed to rich."""
    from rich.console import Console
    from rich.markup import escape

    out = Console(width=40, no_color=True, force_terminal=False)
    with out.capture() as cap:
        out.print(escape(meter(50, 100, width=6)))
    assert "█" in cap.get()


# --------------------------------------------------------------- cpu

def test_cpu_percent_comes_from_load_over_cores():
    assert cpu_pct({"load": [4.0, 0, 0], "cpu_cores": 8}) == 50


def test_cpu_percent_is_unknown_rather_than_zero_without_telemetry():
    """Zero would read as 'idle', which is a different claim from 'we do not know'."""
    assert cpu_pct({"load": None, "cpu_cores": 8}) is None
    assert cpu_pct({"load": [1.0, 0, 0], "cpu_cores": None}) is None


def test_cpu_percent_is_capped_at_a_hundred():
    assert cpu_pct({"load": [32.0, 0, 0], "cpu_cores": 8}) == 100


# --------------------------------------------------------------- gpu

def test_gpu_percent_reports_the_busiest_card():
    """One saturated card makes the box a bad place to send work, however idle its
    siblings are -- and free_vram_mib already takes the max for the same reason."""
    row = {"gpus": [{"util_pct": 12}, {"util_pct": 91}, {"util_pct": 0}]}
    assert gpu_pct(row) == 91


def test_gpu_percent_is_none_without_a_gpu():
    """A CPU box has no utilisation to report, which is not the same as zero."""
    assert gpu_pct({"gpus": []}) is None
    assert gpu_pct({}) is None


def test_gpu_percent_survives_a_card_that_reports_no_utilisation():
    """nvidia-smi returns [N/A] for utilisation on some cards; a KeyError here would
    take down the whole render loop."""
    assert gpu_pct({"gpus": [{"name": "weird"}]}) == 0


# ------------------------------------------------- multi-gpu / multi-disk rows

def _gpu(name="RTX 4090", util=0, free=20000, total=24564):
    return {"name": name, "util_pct": util,
            "vram_total_mib": total, "vram_free_mib": free}


def test_every_gpu_gets_its_own_line():
    """"RTX 4090 x2" hides a heterogeneous box, and one bar over both cards describes
    neither of them."""
    names, utils, vram = gpu_cells(
        _row(gpus=[_gpu("RTX 4090", 12), _gpu("RTX 3090", 98)]))
    assert names.split("\n") == ["RTX 4090", "RTX 3090"]
    assert utils.split("\n") == ["12%", "98%"]
    assert len(vram.split("\n")) == 2


def test_a_single_gpu_device_is_unchanged():
    """A fleet of one-card boxes must look exactly as it did before."""
    names, utils, vram = gpu_cells(_row(gpus=[_gpu(util=34)]))
    assert "\n" not in names and "\n" not in utils and "\n" not in vram


def test_a_gpuless_device_reports_dashes():
    assert gpu_cells(_row(gpus=[])) == ("-", "-", "-")


def test_each_card_gets_a_meter_of_its_own():
    """The old cell summed used/total across every card but printed the *max* free of
    any one of them, so the bar and the number beside it described different hardware.
    """
    _, _, vram = gpu_cells(_row(gpus=[_gpu(free=0, total=24000),
                                      _gpu(free=24000, total=24000)]))
    full, empty = vram.split("\n")
    assert FILLED * 6 in full
    assert EMPTY * 6 in empty


def test_ls_reports_busy_per_card_not_per_box():
    """`ls` marked the whole machine busy if *any* card was, so a box with one
    saturated card and one free one looked entirely unusable."""
    names, vram = gpu_cells_compact(_row(gpus=[
        {"name": "RTX 4090", "vram_free_mib": 1024, "busy": True},
        {"name": "RTX 3090", "vram_free_mib": 24000, "busy": False}]))
    busy, idle = vram.split("\n")
    assert names.split("\n") == ["RTX 4090", "RTX 3090"]
    assert "busy" in busy and "idle" in idle


def test_every_mount_gets_its_own_line():
    """A rental whose / is a full 38G overlay and whose real storage is /workspace
    reported only the roomiest mount, hiding the one about to fill up."""
    cell = disk_cell(_row(disks=[{"mount": "/", "free_gb": 38},
                                 {"mount": "/workspace", "free_gb": 2150}]))
    first, second = cell.split("\n")
    assert "38G" in first and "/" in first
    assert "2.1T" in second and "/workspace" in second


def test_a_lone_root_disk_stays_a_bare_number():
    assert disk_cell(_row(disks=[{"mount": "/", "free_gb": 890}])) == "890G"


def test_a_lone_disk_elsewhere_still_names_its_mount():
    cell = disk_cell(_row(disks=[{"mount": "/volume1", "free_gb": 9200}]))
    assert "9.0T" in cell and "/volume1" in cell


def test_rows_without_a_mount_list_fall_back_to_the_single_figure():
    """Snapshots cached before every mount reached list views carry only the reduced
    number, and must still render."""
    assert disk_cell(_row(disk_free_gb=890, disk_mount="/")) == "890G"
    assert "/volume1" in disk_cell(_row(disk_free_gb=9200, disk_mount="/volume1"))
    assert disk_cell(_row()) == "-"


def test_large_disks_read_as_terabytes():
    """"1648G" is arithmetic; "1.6T" is the answer to the question actually asked."""
    assert disk_cell(_row(disks=[{"mount": "/", "free_gb": 1648}])) == "1.6T"
    assert disk_cell(_row(disks=[{"mount": "/", "free_gb": 890}])) == "890G"


def test_a_long_card_name_cannot_squeeze_the_table():
    """Stripping the vendor prefix is not enough: the pro cards carry another twenty
    characters of marketing after the part anyone reads."""
    out = _rendered([_row(name="box", gpus=[
        _gpu("NVIDIA RTX PRO 6000 Blackwell Workstation Edition")])])
    assert "RTX PRO 6000" in out
    assert "Workstation Edition" not in out


def test_the_vendor_prefix_is_stripped_from_every_card():
    """Only "NVIDIA GeForce " was stripped, so datacentre cards kept a "NVIDIA " that
    says nothing -- and those are exactly the boxes with enough cards to need the room.
    """
    names, _, _ = gpu_cells(_row(gpus=[_gpu("NVIDIA GeForce RTX 4090"),
                                       _gpu("NVIDIA H100 80GB HBM3"),
                                       _gpu("Apple M3 Max")]))
    assert names.split("\n") == ["RTX 4090", "H100 80GB HBM3", "Apple M3 Max"]


def test_a_tall_device_grows_a_gutter_under_its_name():
    """One dim mark says "this line is still the box above" without spending a row."""
    lines = name_cell(_row(name="koa04", gpus=[_gpu(), _gpu()])).split("\n")
    assert len(lines) == 2
    assert "koa04" in lines[0]
    assert "\u2502" in lines[1]


def test_a_short_device_has_no_gutter():
    assert "\n" not in name_cell(_row(name="koa04", gpus=[_gpu()]))


def test_a_row_is_as_tall_as_its_longest_list():
    row = _row(gpus=[_gpu()], disks=[{"mount": "/", "free_gb": 1},
                                     {"mount": "/d", "free_gb": 2},
                                     {"mount": "/e", "free_gb": 3}])
    assert device_lines(row) == 3
    assert device_lines(_row()) == 1


def _rendered(rows) -> str:
    from rich.console import Console

    out = Console(width=200, no_color=True)
    with out.capture() as cap:
        out.print(render_fleet(rows, {"online": 1, "total": 1, "gpus_free": 0,
                                      "hourly_burn": 0}))
    return cap.get()


def _row(**kw):
    base = {"name": "box", "status": "ok", "telemetry_age_s": 0, "alerts": [],
            "gpus": [], "load": None, "cpu_cores": None}
    return {**base, **kw}


def test_the_table_has_a_gpu_utilisation_column():
    out = _rendered([_row(gpus=[{"name": "RTX 4090", "util_pct": 34,
                                 "vram_total_mib": 24564, "vram_free_mib": 23197}])])
    assert "GPU%" in out
    assert "34%" in out


def test_a_box_with_no_gpu_shows_a_dash_not_zero_percent():
    """0% would read as 'a GPU sitting idle', which is a different machine entirely."""
    out = _rendered([_row()])
    assert "0%" not in out.split("NOTE")[0]


# --------------------------------------------------------------- staleness

def test_a_fresh_reading_is_not_labelled_stale():
    assert staleness({"telemetry_age_s": 1}, live_within=10) == ""


def test_an_ageing_reading_says_how_old_it_is():
    """A shared host is polled every five minutes. Showing its number as though it were
    live would be a lie in the one place you would act on it."""
    assert "5m" in staleness({"telemetry_age_s": 300}, live_within=10)


def test_an_unknown_age_is_not_reported_as_zero():
    assert staleness({"telemetry_age_s": None}, live_within=10) == "?"


# --------------------------------------------------------------- scheduling

def test_a_device_is_due_immediately_when_never_probed():
    s = Schedule(interval=2)
    assert [d.name for d in s.due([_dev("a")], now=0)] == ["a"]


def test_a_device_just_probed_is_not_due_again():
    s = Schedule(interval=2)
    s.record("linux:machine-id:a", ok=True, now=0)
    assert s.due([_dev("a")], now=1) == []


def test_a_device_becomes_due_once_the_interval_passes():
    s = Schedule(interval=2)
    s.record("linux:machine-id:a", ok=True, now=0)
    assert [d.name for d in s.due([_dev("a")], now=3)] == ["a"]


def test_a_shared_host_is_never_polled_at_the_live_rate():
    """config.py: shared_min_interval_s exists to never hammer a multi-user cluster.
    The live view must not be the one thing that ignores it."""
    s = Schedule(interval=2, shared_interval=300)
    shared = _dev("koa04", Kind.SHARED)
    s.record(shared.id, ok=True, now=0)
    assert s.due([shared], now=100) == []
    assert [d.name for d in s.due([shared], now=301)] == ["koa04"]


def test_a_failing_device_is_retried_less_and_less_often():
    """A dead host must not be dialled every two seconds for an hour."""
    s = Schedule(interval=2)
    dev = _dev("dead")
    for _ in range(3):
        s.record(dev.id, ok=False, now=0)
    assert s.due([dev], now=5) == [], "backed off past the plain interval"
    assert [d.name for d in s.due([dev], now=100)] == ["dead"]


def test_backoff_has_a_ceiling():
    """Otherwise a host down overnight is never retried once it comes back."""
    s = Schedule(interval=2, max_backoff=60)
    dev = _dev("dead")
    for _ in range(20):
        s.record(dev.id, ok=False, now=0)
    assert [d.name for d in s.due([dev], now=61)] == ["dead"]


def test_backoff_survives_a_host_that_has_been_down_for_days():
    """`2 ** fails` is an unbounded int, and one past DBL_MAX cannot become a float.

    The ceiling bounds the wait but not the counter, so a dead device keeps
    incrementing at one failure per 60s and crosses 1024 in about 17 hours -- exactly
    the overnight case the ceiling exists to serve.
    """
    # 2.0, not 2: cmd_top's --interval is a Typer float option, and int * int would
    # stay exact integer arithmetic and never reach the conversion that fails.
    s = Schedule(interval=2.0, max_backoff=60)
    dev = _dev("gone")
    for _ in range(1100):
        s.record(dev.id, ok=False, now=0)
    assert s._wait_for(dev) == 60
    assert [d.name for d in s.due([dev], now=61)] == ["gone"]


def test_recovering_clears_the_backoff():
    s = Schedule(interval=2)
    dev = _dev("flaky")
    for _ in range(5):
        s.record(dev.id, ok=False, now=0)
    s.record(dev.id, ok=True, now=100)
    assert [d.name for d in s.due([dev], now=103)] == ["flaky"]


# --------------------------------------------------------------- CLI

def _cli(tmp_path, monkeypatch, devices):
    from typer.testing import CliRunner

    from fleet import cli, inventory as inv, store

    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cli, "probe_many", lambda jobs, **kw: {})
    return CliRunner()


def test_top_without_a_terminal_prints_one_frame_and_exits(tmp_path, monkeypatch):
    """An agent is exactly what would run this in a pipe, and a live loop there would
    spin forever. One frame is also genuinely useful for scripting."""
    from fleet.cli import app

    runner = _cli(tmp_path, monkeypatch, [_dev("a")])
    result = runner.invoke(app, ["top"])
    assert result.exit_code == 0, result.output
    assert "a" in result.output


def test_top_rejects_an_unknown_device(tmp_path, monkeypatch):
    from fleet.cli import app

    runner = _cli(tmp_path, monkeypatch, [_dev("a")])
    assert runner.invoke(app, ["top", "nosuchbox"]).exit_code != 0


def test_a_probe_that_blows_up_does_not_take_the_view_down(tmp_path, monkeypatch):
    """One unreachable host must not end the session -- probe_many already isolates
    failures inside the sweep, and the loop around it has to do the same."""
    from fleet import cli
    from fleet.cli import app

    runner = _cli(tmp_path, monkeypatch, [_dev("a")])

    def boom(*a, **k):
        raise OSError("network gone")

    monkeypatch.setattr(cli, "probe_many", boom)
    result = runner.invoke(app, ["top"])
    assert result.exit_code == 0, result.output


# ------------------------------------------------------- second-hand telemetry

def test_a_relayed_row_says_where_it_came_from():
    """Under the access list a machine reaches only what it is granted, so the center
    relays the rest. Worth showing -- it beats a blank row -- but not as though we had
    just measured it."""
    from fleet.top import provenance

    assert provenance(_row()) == "", "our own probe needs no attribution"
    assert provenance(_row(source="broadcast", probed_by="macbook")) == "via macbook"
    assert provenance(_row(source="broadcast")) == "via center", "a sensible default"


def test_the_table_attributes_a_relayed_row():
    out = _rendered([_row(name="far", source="broadcast", probed_by="macbook")])
    assert "via macbook" in out


def test_the_center_is_marked_in_the_table():
    """It only appeared in --json, which stops being tenable once "is the center
    reachable" decides whether a grant happens now or waits."""
    from fleet.top import name_cell

    assert "◆" in name_cell(_row(name="macbook", role="center"))
    assert "◆" not in name_cell(_row(name="oracle"))
    both = name_cell(_row(name="macbook", role="center", is_self=True))
    assert "◆" in both and "←" in both
