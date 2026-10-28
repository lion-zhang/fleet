"""A shared host -- a cluster login -- is probed lightly: no process list, and its SLURM
jobs and partitions instead. Those were read and stored, then shown nowhere."""

from __future__ import annotations

from fleet import cli
from fleet.models import Device, Kind
from fleet.probe.parse import parse_payload
from fleet.render.view import Detail, device_view

_BODY = ("#FLEET v1\nhost.hostname=login01\nhost.slurm=1\n"
         "#SLURM key|value\nmy_jobs|2\nmy_running|1\n"
         "partition|gpu* up 4 mix\npartition|cpu up 12 idle\n#END rc=0\n")


def _row() -> dict:
    dev = Device(id="id:login01", name="login01", kind=Kind.SHARED,
                 endpoints=[{"target": "login01", "user": "me", "port": 22}])
    return device_view(dev, {"status": "ok", "last_probe_at": 1},
                       parse_payload(_BODY).to_dict(), Detail.FULL)


def test_show_json_has_the_jobs_and_partitions_read_in_place_of_processes():
    sl = _row()["slurm"]
    assert sl["my_jobs"] == 2 and sl["my_running"] == 1
    assert sl["partitions"][0] == {"name": "gpu", "default": True, "avail": "up",
                                   "nodes": 4, "state": "mix"}
    assert [p["name"] for p in sl["partitions"]] == ["gpu", "cpu"]


def test_show_prints_them(capsys):
    cli._print_show(_row())
    out = capsys.readouterr().out
    assert "2 job(s) of yours, 1 running" in out
    (gpu,) = [ln for ln in out.splitlines() if ln.strip().startswith("gpu ")]
    assert "4 node(s)" in gpu and "mix" in gpu and "default" in gpu


def test_top_of_the_host_shows_them_too():
    from rich.console import Console

    from fleet.render.top import render_device

    con = Console(width=150, record=True)
    con.print(render_device(_row()))
    out = con.export_text()
    assert "2 job(s) of yours, 1 running" in out and "gpu mix" in out


def test_a_machine_without_slurm_has_none():
    dev = Device(id="id:box", name="box", kind=Kind.PERMANENT,
                 endpoints=[{"target": "box", "user": "me", "port": 22}])
    row = device_view(dev, {"status": "ok", "last_probe_at": 1},
                      parse_payload("#FLEET v1\nhost.hostname=box\n#END rc=0\n").to_dict(),
                      Detail.FULL)
    assert row["slurm"] is None


def test_top_measures_a_shared_host_it_was_named_for(monkeypatch):
    """`fleet top login01` never measured it -- only `probeable` machines were due -- so
    its reading was whatever `fleet ls` left, and the age on screen only grew. A bare
    `fleet top` still leaves it alone."""
    from fleet.config import load_config
    from fleet.ops import rows
    from fleet.render.top import Schedule

    dev = Device(id="id:login01", name="login01", kind=Kind.SHARED,
                 probe_policy="on_demand",
                 endpoints=[{"target": "login01", "user": "me", "port": 22}])
    asked: list[list[str]] = []
    monkeypatch.setattr(rows, "_probe", lambda conn, due, *a, **k: asked.append(
        [d.name for d in due]))
    monkeypatch.setattr(rows, "_render", lambda *a, **k: [])
    cfg = load_config()

    rows.tick(None, [dev], Schedule(), cfg, Detail.FULL)
    assert asked == []
    rows.tick(None, [dev], Schedule(), cfg, Detail.FULL, named=True)
    assert asked == [["login01"]]
