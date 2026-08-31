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
from fleet.top import Schedule, cpu_pct, meter, staleness


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
    monkeypatch.setattr(cli, "maybe_autosync", lambda: None)
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
