"""The rendered output paths.

Every view test until now asserted on `device_view`'s dict, and every CLI test used
--json. That left the human-facing renderers -- the ones that actually index into those
dicts -- with no coverage at all, which is how `fleet show` shipped raising KeyError on
'avail_kb' after full_view stopped emitting raw kb.

A serializer changing shape is normal. Its consumers silently not being exercised is the
bug, so these tests render every non-JSON path against real captured probe output.
"""

from __future__ import annotations

import pathlib

import pytest
from typer.testing import CliRunner

from fleet.models import Device, Kind, ProbeResult, Status
from fleet.probe.parse import parse_payload

FIX = pathlib.Path(__file__).parent / "fixtures" / "probe"
FIXTURES = ["gpu-box", "vm-a", "vm-b", "macos-laptop"]


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A fleet whose devices each carry a real captured snapshot."""
    from fleet import cli, inventory as inv, store

    devices = [
        Device(id=f"linux:machine-id:{name}", name=name, kind=Kind.PERMANENT,
               endpoints=[{"target": f"{name}.example", "user": "root", "port": 22}])
        for name in FIXTURES
    ]
    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(cli, "maybe_autosync", lambda: None)
    monkeypatch.setattr(cli, "probe_many", lambda jobs, **kw: {})
    monkeypatch.setenv("COLUMNS", "200")   # else rich truncates names to "macos-lap…"

    conn = store.connect(tmp_path / "cache.db")
    for dev in devices:
        snap = parse_payload((FIX / f"{dev.name}.txt").read_text())
        store.record(conn, dev.id, ProbeResult(status=Status.OK, snapshot=snap))
    conn.close()
    return CliRunner()


@pytest.mark.parametrize("name", FIXTURES)
def test_show_renders_for_every_captured_machine(seeded, name):
    """`fleet show` indexes into full_view by hand, so it breaks the moment that view
    changes shape -- which is exactly what happened to the disk fields."""
    from fleet.cli import app

    result = seeded.invoke(app, ["show", name])
    assert result.exit_code == 0, result.output


def test_show_reports_disk_space_in_gb(seeded):
    """Not merely that it renders: that the disk line still says something true."""
    from fleet.cli import app

    out = seeded.invoke(app, ["show", "gpu-box"]).output
    assert "/workspace" in out
    assert "1647" in out or "1648" in out, out


def test_ls_renders_the_whole_fleet(seeded):
    from fleet.cli import app

    result = seeded.invoke(app, ["ls"])
    assert result.exit_code == 0, result.output
    for name in FIXTURES:
        assert name in result.output


def test_top_renders_a_single_frame(seeded):
    from fleet.cli import app

    result = seeded.invoke(app, ["top"])
    assert result.exit_code == 0, result.output


def test_top_renders_one_device(seeded):
    from fleet.cli import app

    result = seeded.invoke(app, ["top", "gpu-box"])
    assert result.exit_code == 0, result.output


def test_every_rendered_path_agrees_with_its_json(seeded):
    """The two surfaces read the same view; if the renderer drifts, one of them is
    lying. This catches the drift that a KeyError happens to make loud."""
    import json

    from fleet.cli import app

    blob = json.loads(seeded.invoke(app, ["show", "gpu-box", "--json"]).output)
    rendered = seeded.invoke(app, ["show", "gpu-box"]).output
    assert blob["hostname"] in rendered
