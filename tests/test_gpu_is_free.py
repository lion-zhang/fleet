"""Whether a GPU has room: one with no memory of its own, and one held out of sight.

On DGX Spark's GB10 nvidia-smi reports memory as [N/A]. Read as 0, `fleet ls` said
"0.0G idle" and `fleet ps` "(0G free)": a card nothing fits on, on a machine with most
of its 128G free for it. And a card whose memory a job in a container held, listing no
process, read "idle" and counted as free beside the alert saying not to.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

import pytest

from fleet.models import Device, Kind
from fleet.probe.parse import parse_payload
from fleet.render import top
from fleet.render.view import Detail, device_view, jobs_view

FIX = pathlib.Path(__file__).parent / "fixtures" / "probe" / "gpu-box.txt"
CARD = "0|GPU-00000000-1111-2222-3333-444444444444|NVIDIA GeForce RTX 4090|24564|853|23197|0|47|28.47"
GB10 = "0|GPU-00000000-1111-2222-3333-444444444444|NVIDIA GB10|[N/A]|[N/A]|[N/A]|0|41|4.95"


def _row(line, detail=Detail.COMPACT):
    text = FIX.read_text(encoding="utf-8")
    assert CARD in text
    snap = parse_payload(text.replace(CARD, line))
    dev = Device(id="linux:machine-id:" + snap.machine_id, name="spark", kind=Kind.PERMANENT,
                 endpoints=[{"target": "h", "user": "u", "port": 22}])
    state = {"status": "ok", "last_probe_at": snap.ts, "last_ok_at": snap.ts}
    return device_view(dev, state, snap.to_dict(), detail=detail)


def test_a_card_that_shares_memory_says_so_instead_of_none_free():
    row = _row(GB10)
    assert row["gpus"][0]["shared_memory"] is True
    _, vram = top.gpu_cells_compact(row)
    assert vram.startswith("shared ") and "0.0G" not in vram
    assert "vram-" not in " ".join(row["facts"])
    idle = jobs_view([_row(GB10, Detail.FULL)])["idle"]
    assert idle and idle[0]["shared_memory"] is True


def test_a_card_with_its_own_memory_is_unchanged():
    row = _row(CARD)
    assert row["gpus"][0]["shared_memory"] is False
    _, vram = top.gpu_cells_compact(row)
    assert vram.startswith("22.7G ")
    assert "vram-24g" in row["facts"]


def test_a_card_held_by_a_process_we_cannot_see_is_not_free():
    # A job in a container: nvidia-smi lists no process, and 70G of the card is in use.
    row = _row("0|GPU-00000000-1111-2222-3333-444444444444|NVIDIA A100-SXM4-80GB"
               "|81920|71680|10240|0|33|61.2", Detail.FULL)
    assert row["gpus"][0]["busy"] is True
    assert not jobs_view([row])["idle"]


def test_fleet_ps_says_a_card_is_busy_when_it_cannot_see_the_job():
    # Neither a job nor idle, it was left out of both: "No jobs on any GPU." beside a
    # card holding 70G, and an agent reading --json was told nothing about it.
    row = _row("0|GPU-00000000-1111-2222-3333-444444444444|NVIDIA A100-SXM4-80GB"
               "|81920|71680|10240|97|33|61.2", Detail.FULL)
    view = jobs_view([row])
    assert view["busy_unseen"] == [{"machine": "spark", "gpu": 0,
                                    "gpu_name": "NVIDIA A100-SXM4-80GB",
                                    "vram_used_mib": 71680, "util_pct": 97,
                                    "shared_host": False}]
    text = " ".join(str(p) for p in top.render_ps(view))
    assert "No jobs on any GPU" not in text
    assert "spark gpu0" in text and "70.0G, 97%" in text
    assert jobs_view([_row(CARD, Detail.FULL)])["busy_unseen"] == []


def test_a_desktops_own_windows_do_not_make_a_card_busy():
    # The captured box: 414 MiB its desktop holds that nvidia-smi does not list.
    assert _row(CARD)["gpus"][0]["busy"] is False


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("ps"),
                    reason="runs payload.sh")
def test_whose_job_it_is_is_not_cut_to_eight_characters(tmp_path):
    # procps cuts a user name past 8 characters to "research+" whenever another column
    # follows it, and `fleet ps` named the owner of a GPU job "research+". The stand-in
    # ps answers as procps does; this test's own pid is the job.
    from test_resources import make_home, probe

    me, real = os.getpid(), shutil.which("ps")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "nvidia-smi").write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        "  *driver_version*) echo 550.54.15 ;;\n"
        "  *query-gpu=index*) echo '0, GPU-a, NVIDIA A100, 81920, 20000, 61920, 90, 40, 300' ;;\n"
        f"  *query-compute-apps*) echo 'GPU-a, {me}, 20000' ;;\n"
        "esac\n")
    (bin_ / "ps").write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        f"  *etimes*-p\\ {me}*) case \"$*\" in\n"
        "      *user:64*) echo 'researcher1    12 python3' ;;\n"
        "      *) echo 'research+    12 python3' ;;\n"
        "    esac ;;\n"
        f"  *) exec {real} \"$@\" ;;\nesac\n")
    for tool in bin_.iterdir():
        tool.chmod(0o755)
    snap = parse_payload(probe(make_home(tmp_path), PATH=f"{bin_}:{os.environ['PATH']}"))
    (job,) = [p for p in snap.processes if p.scope == "gpu"]
    assert job.user == "researcher1"
