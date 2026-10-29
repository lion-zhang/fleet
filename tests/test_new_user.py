"""What a new user ran into in a first week of using fleet the way its guides say to.

Each test is one thing that went wrong in ordinary use: a tag written the way people
write tags, the first `fleet show` of the machine fleet runs on, `fleet doctor` on the
machine `fleet add` just refused, a share measured in petabytes.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from fleet import cli, notes
from fleet.models import Device, Kind
from fleet.ops import doctor
from fleet.probe.parse import parse_payload
from fleet.render import view
from fleet.render.top import render_device, render_ps
from fleet.ssh import cmd as ssh_cmd
from fleet.state import inventory as inv
from fleet.state import store, writes


def _text(renderable, width: int = 140) -> str:
    c = Console(width=width, record=True, color_system=None)
    c.print(renderable)
    return c.export_text()


def _row(name: str, **kw) -> dict:
    return {"name": name, "status": "ok", "kind": "permanent", "gpus": [], "facts": [],
            "tags": [], "free_vram_mib": 0, **kw}


# ------------------------------------------------------------------ tags
def test_a_comma_separated_tag_is_several_tags(tmp_path, monkeypatch):
    """`--tag training,big` was stored as the one tag "training,big", which no
    `--tag training` ever found."""
    inv.save([Device(id="id:box", name="box", kind=Kind.PERMANENT)])
    r = CliRunner().invoke(cli.app, ["edit", "box", "--tag", "Training,big", "--tag", "lab gpu",
                                     "--local"])
    assert r.exit_code == 0, r.output
    assert inv.load()[0].tags == ["training", "big", "lab", "gpu"]


def test_a_tag_stored_whole_before_tags_were_split_can_still_be_removed():
    inv.save([Device(id="id:box", name="box", kind=Kind.PERMANENT, tags=["training,big", "lab"])])
    r = CliRunner().invoke(cli.app, ["edit", "box", "--untag", "training,big", "--local"])
    assert r.exit_code == 0, r.output
    assert inv.load()[0].tags == ["lab"]


def test_ls_reads_a_comma_separated_filter_as_each_tag(monkeypatch):
    monkeypatch.setattr(cli, "_rows", lambda *a, **k: [
        _row("a", tags=["training", "big"]), _row("b", tags=["training"])])
    inv.save([Device(id="id:a", name="a"), Device(id="id:b", name="b")])
    r = CliRunner().invoke(cli.app, ["ls", "--json", "--tag", "training,big"])
    assert r.exit_code == 0, r.output
    assert [d["name"] for d in json.loads(r.stdout)["devices"]] == ["a"]


def test_a_filter_that_matches_nothing_does_not_say_the_list_is_empty(monkeypatch):
    """"No devices yet. Add one" with two machines in the list."""
    monkeypatch.setattr(cli, "_rows", lambda *a, **k: [_row("a", tags=["lab"])])
    inv.save([Device(id="id:a", name="a")])
    r = CliRunner().invoke(cli.app, ["ls", "--tag", "prod"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.output.split())
    assert "No devices yet" not in out
    assert "No machine matches --tag prod" in out


def test_a_tagged_machine_with_no_reading_is_counted_as_not_considered(monkeypatch):
    """`--tag cuda` cannot be judged on a machine that was never read, tags or no tags;
    counting only the untagged ones left it out of the count as well as the list."""
    monkeypatch.setattr(cli, "_rows", lambda *a, **k: [
        _row("read", facts=["cuda"]), _row("unread", tags=["lab"]),
        _row("unread-but-tagged", tags=["cuda"])])
    inv.save([Device(id=f"id:{n}", name=n) for n in ("read", "unread", "unread-but-tagged")])
    r = CliRunner().invoke(cli.app, ["ls", "--json", "--tag", "cuda"])
    assert r.exit_code == 0, r.output
    assert {d["name"] for d in json.loads(r.stdout)["devices"]} == {"read", "unread-but-tagged"}
    assert "1 with no telemetry was not considered" in " ".join(r.stderr.split())


# ------------------------------------------------------------------ this machine
def test_the_machine_fleet_runs_on_is_not_said_to_lack_an_endpoint():
    here = Device(id="id:here", name="laptop", kind=Kind.PERMANENT)
    assert view.connect_view(here, here=True)["hint"] == "this machine: run commands here"
    assert view.connect_view(here)["hint"] == "no endpoint recorded"


# ------------------------------------------------------------------ doctor
def test_doctor_looks_at_a_machine_add_refused(monkeypatch):
    """`fleet add` records nothing when it cannot get in, and sends people to `fleet
    doctor NAME`, which answered "No machine named" for exactly those machines."""
    seen = []

    def diagnose(dev, **k):
        seen.append(dev)
        return doctor.advise(dev, "auth_failed", "denied")

    monkeypatch.setattr(doctor, "diagnose", diagnose)
    monkeypatch.setattr(ssh_cmd.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "hostname 10.0.0.5\nuser me\nport 22\n", ""))
    r = CliRunner().invoke(cli.app, ["doctor", "me@10.0.0.5", "--json"])
    assert r.exit_code == 0, r.output
    (dev,) = seen
    assert dev.endpoints[0]["target"] == "10.0.0.5" and dev.endpoints[0]["user"] == "me"
    assert inv.live(inv.load()) == []                     # and it is still not added


# ------------------------------------------------------------------ sizes and disks
def test_a_petabyte_share_reads_in_petabytes():
    from fleet.render.top import _gb

    assert _gb(1024 ** 2) == "1.0P" and _gb(2048) == "2.0T" and _gb(512) == "512G"


def test_one_machine_shows_a_read_only_mount_as_read_only_not_full():
    out = _text(render_device({"name": "box", "disks": [
        {"mount": "/", "use_pct": 40, "free_gb": 2048, "writable": True},
        {"mount": "/opt/vendor", "use_pct": 100, "free_gb": 0, "writable": False}]}))
    assert "2.0T free" in out and "2048G" not in out
    assert "read-only  /opt/vendor" in out
    assert not [ln for ln in out.splitlines() if "/opt/vendor" in ln and "free" in ln]


# ------------------------------------------------------------------ ps
def test_a_job_the_login_cannot_see_is_not_drawn_as_just_started():
    """A process in a container has no user, age or command from outside it: "0m" and a
    blank said it had just started and was nothing."""
    job = {"machine": "gpubox", "gpu": 0, "gpu_name": "NVIDIA RTX 4090", "pid": 4242,
           "user": "", "vram_mib": 20480, "runtime_s": 0, "command": ""}
    out = " ".join(_text(render_ps({"jobs": [job], "idle": [], "not_read": []})[0]).split())
    assert "pid 4242, not visible from this login" in out
    assert " 0m " not in out


# ------------------------------------------------------------------ the machine's notes
def test_notes_over_the_limit_are_refused_rather_than_cut():
    """Cut at 8 KB the file was a different file -- a list ending early -- taken as what
    the machine said, without a word."""
    big = base64.b64encode(b"x" * (notes.MAX_BYTES + 1)).decode()
    with pytest.raises(notes.NotesError, match="larger than 8 KB"):
        notes.decode(big)
    _, error = notes.from_probe({"notes.user": big})
    assert "larger than 8 KB" in error
    assert notes.decode(base64.b64encode(b"x" * notes.MAX_BYTES).decode()) == "x" * notes.MAX_BYTES


def test_a_folder_kind_written_with_a_capital_is_the_same_kind():
    """`Models:` written by hand was missing from `fleet models`."""
    body = ("#FLEET v1\nhost.hostname=box\n#STORE kind|path|exists|avail_kb|size_kb|sized_at\n"
            "Models|/srv/models|1|100|50|1\n#END rc=0\n")
    (st,) = parse_payload(body).to_dict()["storage"]
    assert st["kind"] == "models"


# ------------------------------------------------------------------ software
def test_a_light_reading_keeps_the_software_it_did_not_read():
    """Windows skips the software scan in a light probe, so one `fleet top` there left
    `fleet show` listing no software at all."""
    prev = {"software": {"python": "3.12.1"}, "software_at": 100}
    snap = {"software": {}}
    store.carry_over(prev, snap)
    assert snap["software"] == {"python": "3.12.1"} and snap["software_at"] == 100

    fresh = {"software": {"python": "3.13.0"}, "software_at": 200}
    store.carry_over(prev, fresh)
    assert fresh["software"] == {"python": "3.13.0"}


def test_the_compact_software_view_names_the_newest_version():
    """The first environment read won, so a stale venv's torch was the answer on a box
    that also had a newer one."""
    snap = {"software": {}, "pyenvs": [
        {"env": "/old", "python": "3.10", "package": "torch", "version": "2.9.1"},
        {"env": "/new", "python": "3.12", "package": "torch", "version": "2.10.0+cu124"}]}
    assert view.software_view(snap, full=False)["torch"] == "2.10.0+cu124"


# ------------------------------------------------------------------ setup
@pytest.mark.parametrize("target", ["claude-desktop", "windsurf"])
def test_project_setup_is_refused_for_an_app_with_one_config(tmp_path, monkeypatch, target):
    """It wrote the desktop app's config into the repo, where no app looks, and said
    "created"."""
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli.app, ["setup", "--target", target, "--project"])
    assert r.exit_code == 2, r.output
    assert "not one per project" in " ".join(r.output.split())
    assert not any(tmp_path.iterdir())


def test_project_setup_for_vscode_writes_the_workspace_config(tmp_path, monkeypatch):
    """VS Code reads .vscode/mcp.json in a workspace, as agents.md says, but it was
    refused as keeping "one config for you, not one per project"."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    runner = CliRunner()
    r = runner.invoke(cli.app, ["setup", "--target", "vscode", "--project"])
    assert r.exit_code == 0, r.output
    path = tmp_path / ".vscode" / "mcp.json"
    assert json.loads(path.read_text())["servers"]["fleet"]["args"][-1] == "mcp"
    assert not (tmp_path / "home").exists()
    r = runner.invoke(cli.app, ["setup", "--target", "vscode", "--project", "--uninstall"])
    assert r.exit_code == 0, r.output
    assert "fleet" not in json.loads(path.read_text())["servers"]


# ------------------------------------------------------------------ the write queue
def test_a_ticket_left_with_our_own_pid_by_an_earlier_run_is_not_ours():
    """In a container every run can be pid 1, and a ticket a killed run left behind made
    the next one wait on itself until the ticket went stale."""
    me = writes._start_time(os.getpid())
    assert writes._alive({"pid": os.getpid(), "started": me})
    assert not writes._alive({"pid": os.getpid(), "started": me - 3600})
    assert writes._alive({"pid": os.getpid()})        # an old ticket with no start time


# ------------------------------------------------------------------ edit --ssh
def _answering(monkeypatch, machine_id: str | None):
    from fleet.models import ProbeResult, Snapshot, Status

    res = (ProbeResult(status=Status.UNREACHABLE) if machine_id is None else
           ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="laptop", os="Ubuntu",
                                                           machine_id=machine_id)))
    monkeypatch.setattr(cli, "run_probe", lambda ep, **k: res)
    monkeypatch.setattr(ssh_cmd.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "hostname 10.0.0.9\nuser me\nport 2222\n", ""))


def test_a_new_address_that_reaches_another_machine_is_refused(monkeypatch):
    """A port typo pointed gpubox at the laptop, and from then on `fleet ls` showed
    gpubox with no GPUs and `fleet ssh gpubox` opened the laptop."""
    inv.save([Device(id="linux:machine-id:aaaa", name="gpubox", kind=Kind.PERMANENT,
                     endpoints=[{"target": "10.0.0.5", "user": "me", "port": 22}]),
              Device(id="linux:machine-id:bbbb", name="laptop", kind=Kind.PERMANENT)])
    _answering(monkeypatch, "bbbb")
    r = CliRunner().invoke(cli.app, ["edit", "gpubox", "--ssh", "ssh -p 2222 me@10.0.0.9"])
    assert r.exit_code == 2, r.output
    assert "reaches laptop, not gpubox" in " ".join(r.output.split())
    assert inv.find_exact(inv.load(), "linux:machine-id:aaaa").endpoints[0]["target"] == "10.0.0.5"


@pytest.mark.parametrize("answer", [None, "aaaa"], ids=["down", "itself"])
def test_a_new_address_that_is_down_or_reaches_the_machine_is_taken(monkeypatch, answer):
    inv.save([Device(id="linux:machine-id:aaaa", name="gpubox", kind=Kind.PERMANENT,
                     endpoints=[{"target": "10.0.0.5", "user": "me", "port": 22}])])
    _answering(monkeypatch, answer)
    r = CliRunner().invoke(cli.app, ["edit", "gpubox", "--ssh", "ssh -p 2222 me@10.0.0.9"])
    assert r.exit_code == 0, r.output
    assert inv.load()[0].endpoints[0]["target"] == "10.0.0.9"


def _hidden_job_row() -> dict:
    from fleet.render.view import Detail, device_view

    body = ("#FLEET v1\nhost.hostname=gpubox\n"
            "#GPU idx|uuid|name|total_mib|used_mib|free_mib|util_pct|temp_c|power_w\n"
            "0|GPU-0|NVIDIA GeForce RTX 4090|24564|20100|4464|97|60|300\n"
            "#GPUPROC gpu_uuid|pid|vram_mib|user|etimes|comm|args\n"
            "GPU-0|7919|20000||||\n#END rc=0\n")
    dev = Device(id="id:gpubox", name="gpubox", kind=Kind.PERMANENT,
                 endpoints=[{"target": "gpubox", "user": "me", "port": 22}])
    return device_view(dev, {"status": "ok", "last_probe_at": 1},
                       parse_payload(body).to_dict(), Detail.FULL)


def test_show_and_top_say_a_job_cannot_be_seen_rather_than_that_it_just_started(capsys):
    """`fleet show` and `fleet top NAME` drew the same container job as `fleet ps` did:
    "?", "0m" and nothing."""
    row = _hidden_job_row()
    cli._print_show(row)
    shown = capsys.readouterr().out
    (line,) = [ln for ln in shown.splitlines() if "7919" in ln]
    assert "not visible from this login" in line and "0m" not in line
    assert "pid 7919, not visible from this login" in _text(render_device(row))


def test_a_job_the_login_cannot_see_has_no_runtime_in_json():
    from fleet.render.view import jobs_view

    (job,) = jobs_view([_hidden_job_row()])["jobs"]
    assert job["pid"] == 7919 and job["runtime_s"] is None


def test_show_of_a_machine_never_reached_ends_on_what_it_knows(capsys):
    """A machine that never answered has no OS or arch yet: its header read
    "permanent · timeout · ? · " and stopped there."""
    from fleet.render.view import Detail, device_view

    dev = Device(id="id:quiet", name="quiet", kind=Kind.PERMANENT,
                 endpoints=[{"target": "quiet", "user": "me", "port": 22}])
    row = device_view(dev, {"status": "timeout", "last_probe_at": 1}, None, Detail.FULL)
    cli._print_show(row)
    (head,) = [ln for ln in capsys.readouterr().out.splitlines() if "quiet" in ln][:1]
    assert head.rstrip().endswith("permanent · timeout")


@pytest.mark.parametrize("argv", [["--target", "nosuch"], ["--target", "Claude"],
                                  ["--target", "claude-desktop", "--project"]])
def test_setup_refuses_a_target_before_recording_this_machine(tmp_path, monkeypatch, argv):
    """`fleet setup --target nosuch` recorded this machine and said "✓ fleet is ready
    here" before it said there is no such target."""
    monkeypatch.chdir(tmp_path)
    ran = []
    monkeypatch.setattr(cli, "_first_run", lambda *a, **k: ran.append(a))
    r = CliRunner().invoke(cli.app, ["setup", *argv])
    assert r.exit_code == 2, r.output
    assert not ran
    assert "ready here" not in r.output


@pytest.mark.parametrize("argv", [["add", "ssh x", "--kind", "weird"], ["add", "ssh x", "--self"],
                                  ["add", "ssh x", "--name", ""], ["ls", "--tag", ""]])
def test_a_refused_command_does_not_record_this_machine_first(tmp_path, monkeypatch, argv):
    """`fleet add ... --kind weird` said "✓ fleet is ready here: recorded vm" and then
    that weird is not a kind."""
    ran = []
    monkeypatch.setattr(cli, "_first_run", lambda *a, **k: ran.append(a))
    r = CliRunner().invoke(cli.app, argv)
    assert r.exit_code == 2, r.output
    assert not ran


def test_an_old_reading_says_how_old_it_is(capsys):
    """A mobile device is never probed, so `fleet show` drew a reading hours old as
    though it had just been taken; `fleet ls` gave its age as "7200s"."""
    import time

    from fleet.render.top import _ls_table
    from fleet.render.view import Detail, device_view

    dev = Device(id="id:phone", name="phone", kind=Kind.MOBILE, probe_policy="never",
                 endpoints=[{"target": "phone", "user": "me", "port": 22}])
    old = device_view(dev, {"status": "ok", "last_probe_at": int(time.time()) - 7200},
                      None, Detail.FULL)
    cli._print_show(old)
    assert "as read 2h ago; fleet never probes a mobile device" in capsys.readouterr().out
    assert " 2h " in _text(_ls_table([old], set()))
    fresh = device_view(dev, {"status": "ok", "last_probe_at": int(time.time())},
                        None, Detail.FULL)
    cli._print_show(fresh)
    assert "as read" not in capsys.readouterr().out
