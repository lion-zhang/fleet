"""A cluster's login node as fleet sees it: shared, with SLURM, and its GPUs someone's."""

from __future__ import annotations

import os
import shutil
import sys
import time

import pytest

from fleet import cli
from fleet.models import Device, Kind
from fleet.probe.parse import parse_payload
from fleet.render import top
from fleet.render.view import Detail, device_view, jobs_view, slurm_view
from test_gpu_is_free import CARD, FIX
from test_resources import make_home, probe

posix = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                           reason="runs payload.sh")


def _slurm(tmp_path, squeue: str) -> dict:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "squeue").write_text("#!/bin/sh\n" + squeue)
    (bin_ / "sinfo").write_text("#!/bin/sh\necho 'gpu up 4 idle'\n")
    for tool in bin_.iterdir():
        tool.chmod(0o755)
    text = probe(make_home(tmp_path), PATH=f"{bin_}:{os.environ['PATH']}", FLEET_MODE="shared")
    return slurm_view(parse_payload(text).to_dict())


@posix
def test_a_controller_that_does_not_answer_is_not_no_jobs(tmp_path):
    # squeue failing printed nothing, and nothing counted as "0 job(s) of yours": a
    # login node whose controller was down said you had nothing queued.
    assert _slurm(tmp_path, "echo 'slurm_load_jobs error' >&2\nexit 1\n")["my_jobs"] is None


@posix
def test_your_jobs_are_counted(tmp_path):
    sl = _slurm(tmp_path, "printf 'RUNNING\\nPENDING\\nRUNNING\\n'\n")
    assert (sl["my_jobs"], sl["my_running"]) == (3, 2)


@posix
@pytest.mark.skipif(not shutil.which("timeout"), reason="needs timeout")
def test_a_controller_that_hangs_does_not_hang_the_probe(tmp_path):
    # A swamped controller held squeue for minutes, and the whole reading with it,
    # until fleet gave up on the machine as not answering.
    t = time.monotonic()
    sl = _slurm(tmp_path, "exec sleep 60\n")
    assert time.monotonic() - t < 30
    assert sl["my_jobs"] is None


def _login_row() -> dict:
    busy = "0|GPU-00000000-1111-2222-3333-444444444444|NVIDIA A100-SXM4-80GB|81920|20000|61920|97|61|250.1"
    snap = parse_payload(FIX.read_text(encoding="utf-8").replace(CARD, busy))
    dev = Device(id="linux:machine-id:x", name="login1", kind=Kind.SHARED,
                 endpoints=[{"target": "login1.example.ts.net", "user": "lin", "port": 22}])
    return device_view(dev, {"status": "ok"}, snap.to_dict(), Detail.FULL)


def test_a_shared_hosts_busy_gpu_is_not_no_jobs(capsys):
    # Its processes are left unread on purpose, so its card at 97% holding 20G came
    # out of `fleet ps` as "No jobs on any GPU." and of `fleet show` as "none".
    row = _login_row()
    view = jobs_view([row])
    assert [(b["machine"], b["shared_host"]) for b in view["busy_unseen"]] == [("login1", True)]
    text = " ".join(str(p) for p in top.render_ps(view))
    assert "No jobs on any GPU" not in text and "a container?" not in text
    assert "login1 gpu0" in text and "shared host" in text
    cli._print_show(row)
    out = capsys.readouterr().out
    assert "no job is using this GPU" not in out and "shared host" in out
