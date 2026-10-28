"""A GPU that shares the machine's memory is not a card with none.

On DGX Spark's GB10 nvidia-smi reports memory as [N/A]. Read as 0, `fleet ls` said
"0.0G idle" and `fleet ps` "(0G free)": a card nothing fits on, on a machine with most
of its 128G free for it.
"""

from __future__ import annotations

import pathlib

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
