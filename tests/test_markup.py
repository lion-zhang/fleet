"""What a machine says about itself is printed as text, never read as Rich markup.

Tags, notes and paths come from the machine's own machine.yaml -- which a rental
provider's image, or anyone with a shell there, can write -- and error lines, process
names and the OS string come from its programs. One `[/]` in any of them stopped
`fleet ls` for the whole fleet with a MarkupError, and a `[link=...]` made a link.
"""

from __future__ import annotations

import pathlib

import pytest
from rich.console import Console

from fleet.models import Device, Kind
from fleet.probe.parse import parse_payload
from fleet.render.top import render_device, render_fleet, render_ls, render_models, render_ps
from fleet.render.view import Detail, device_view, jobs_view, models_view

FIX = pathlib.Path(__file__).parent / "fixtures" / "probe"
BAD = "[/]x[bold]y[link=https://example.invalid]z"


def _poison(value):
    """Every string in a snapshot with markup that would fail or style if it were read."""
    if isinstance(value, str):
        return value + BAD
    if isinstance(value, list):
        return [_poison(v) for v in value]
    if isinstance(value, dict):
        return {k: _poison(v) for k, v in value.items()}
    return value


def _snap(fixture: str) -> dict:
    snap = _poison(parse_payload((FIX / fixture).read_text(encoding="utf-8")).to_dict())
    snap["notes"] = {"tags": [BAD], "notes": BAD, "paths": {"models": "/data/" + BAD}}
    snap["storage"] = [{"kind": "models", "path": "/data/" + BAD, "exists": False}]
    snap["gpu_error"] = BAD
    return snap


def _rows(detail):
    dev = Device(id="id:x", name="box", kind=Kind.PERMANENT, tags=[BAD],
                 endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])
    failed = Device(id="id:y", name="down", kind=Kind.PERMANENT,
                    endpoints=[{"target": "10.0.0.8", "user": "me", "port": 22}])
    return [device_view(dev, {"status": "ok", "last_probe_at": 1}, _snap(f), detail)
            for f in ("gpu-box.txt", "macos-laptop.txt", "vm-a.txt")] + [
        device_view(failed, {"status": "probe_error", "last_probe_at": 1,
                             "error_detail": "sh: " + BAD + ": not found"}, None, detail)]


def _printed(renderable) -> str:
    c = Console(width=200, record=True, color_system=None)
    for r in renderable if isinstance(renderable, list) else [renderable]:
        c.print(r)
    return c.export_text()


@pytest.mark.parametrize("render", ["ls", "fleet", "device", "ps", "models"])
def test_markup_from_a_machine_is_printed_as_written(render):
    rows = _rows(Detail.FULL if render in ("device", "ps", "models") else Detail.COMPACT)
    out = _printed({
        "ls": lambda: render_ls(rows),
        "fleet": lambda: render_fleet(rows, {}),
        "device": lambda: [render_device(r) for r in rows],
        "ps": lambda: render_ps(jobs_view(rows)),
        "models": lambda: render_models(models_view(rows)),
    }[render]())
    # Printed literally (a cell may cut it short), never taken as a tag.
    assert "[/]" in out or "[bold]" in out or render in ("ps", "models")


@pytest.fixture
def recorded():
    """A machine whose last reading carries markup in everything it said about itself."""
    from fleet.models import ProbeResult, Snapshot, Status
    from fleet.state import inventory as inv
    from fleet.state import store

    inv.save([Device(id="id:x", name="box", kind=Kind.PERMANENT, probe_policy="never",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    snap = parse_payload((FIX / "gpu-box.txt").read_text(encoding="utf-8"))
    snap.notes = {"tags": [BAD], "notes": BAD, "paths": {"models": "/data/" + BAD}}
    snap.os = "Linux " + BAD
    conn = store.connect()
    try:
        store.record(conn, "id:x", ProbeResult(status=Status.OK, snapshot=snap))
    finally:
        conn.close()


@pytest.mark.parametrize("argv", [["ls"], ["show", "box", "--no-refresh"],
                                  ["ps"], ["models"]])
def test_commands_print_a_machine_s_markup_as_written(recorded, argv):
    from typer.testing import CliRunner

    from fleet import cli

    r = CliRunner().invoke(cli.app, argv)
    assert r.exception is None or isinstance(r.exception, SystemExit), r.output
    assert r.exit_code == 0, r.output


def test_doctor_prints_a_machine_s_error_as_written(monkeypatch):
    from typer.testing import CliRunner

    from fleet.probe import runner

    from fleet import cli
    from fleet.models import ProbeResult, Status
    from fleet.state import inventory as inv
    from fleet.state import store

    inv.save([Device(id="id:x", name="box", kind=Kind.PERMANENT, probe_policy="never",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    conn = store.connect()
    try:
        store.record(conn, "id:x", ProbeResult(status=Status.PROBE_ERROR,
                                               error_detail="sh: " + BAD + ": not found"))
    finally:
        conn.close()
    monkeypatch.setattr(runner, "probe_endpoints", lambda eps, **k: ProbeResult(
        status=Status.PROBE_ERROR, error_detail="sh: " + BAD + ": not found"))
    r = CliRunner().invoke(cli.app, ["doctor", "box"])
    assert "[/]x[bold]y" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit), r.output


ESC = "\x1b[2J\x1b]0;owned\x07\x1b[31m"


def test_an_escape_sequence_in_a_machines_own_notes_never_reaches_the_terminal():
    from fleet import notes
    got = notes.check({"notes": "rent me" + ESC, "tags": ["a" + ESC + "b"],
                       "paths": {"models": "/data/" + ESC + "m"}})
    flat = repr(got)
    assert "\\x1b" not in flat and "\\x07" not in flat
    assert got["notes"].startswith("rent me")
    assert got["tags"][0].startswith("a") and got["tags"][0].endswith("b")


def test_an_escape_sequence_in_what_another_user_runs_never_reaches_the_terminal():
    text = (FIX / "gpu-box.txt").read_text(encoding="utf-8").replace(
        "|msedge", "|msedge" + ESC, 1)
    snap = parse_payload(text).to_dict()
    flat = repr(snap)
    assert "\\x1b" not in flat and "\\x07" not in flat
    assert "msedge" in flat
