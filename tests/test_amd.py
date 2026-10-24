"""AMD GPUs, read with amd-smi or rocm-smi as the probe would on a real machine.

payload.sh runs for real, against stand-ins for the two tools that print what each one
prints with --json: the shapes in AMD's documentation, values included as the strings,
units and nested {"value", "unit"} pairs they come as. A job's pid is this test's own,
so `ps` finds a real process to describe.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from fleet.models import Device, Kind
from fleet.probe.parse import parse_payload
from fleet.render.view import device_view, facts

from test_resources import make_home, probe

posix = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh")
                           or not shutil.which("python3"), reason="runs payload.sh")
ME = os.getpid()


def _tool(bin_: Path, name: str, answers: dict[str, object]) -> None:
    """A stand-in for NAME: prints the JSON for the first argument it was given that
    names an answer (`static`, `--showpids`...), and nothing for anything else."""
    data = bin_ / f"{name}.json"
    data.write_text(json.dumps(answers))
    script = bin_ / name
    script.write_text(
        "#!/bin/sh\n"
        f'exec python3 -c \'import json,sys; a=json.load(open("{data}")); '
        'k=next((x for x in sys.argv[1:] if x in a), None); '
        'print(json.dumps(a[k]) if k else "")\' "$@"\n')
    script.chmod(0o755)


def _bin(tmp_path: Path) -> Path:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    return bin_


ROCM_SMI = {
    "--showuniqueid": {
        "card0": {"Unique ID": "0x18f68e602b8a790f", "Card Series": "AMD Instinct MI210",
                  "VRAM Total Memory (B)": "68702699520",
                  "VRAM Total Used Memory (B)": "17179869184", "GPU use (%)": "87",
                  "Temperature (Sensor edge) (C)": "51.0",
                  "Average Graphics Package Power (W)": "212.0"},
        "card1": {"Unique ID": "0x2a7b9c11d0e4f533", "Card Series": "AMD Instinct MI210",
                  "VRAM Total Memory (B)": "68702699520",
                  "VRAM Total Used Memory (B)": "10485760", "GPU use (%)": "0",
                  "Temperature (Sensor edge) (C)": "34.0",
                  "Average Graphics Package Power (W)": "41.0"},
        "system": {"Driver version": "6.8.5"},
    },
    "--showpids": {"system": {f"PID{ME}": "python3, 1, 17179869184, 0, 0"}},
    "--showpidgpus": {"system": {f"PID{ME}": "[0]"}},
}

AMD_SMI = {
    "static": [{"gpu": 0, "asic": {"market_name": "AMD Radeon PRO W7900",
                                   "asic_serial": "0xA1B2C3D4E5F60718"}}],
    "metric": [{"gpu": 0,
                "usage": {"gfx_activity": {"value": 64, "unit": "%"}},
                "power": {"socket_power": {"value": 188, "unit": "W"}},
                "temperature": {"edge": {"value": 58, "unit": "C"}},
                "mem_usage": {"total_vram": {"value": 49136, "unit": "MB"},
                              "used_vram": {"value": 20480, "unit": "MB"},
                              "free_vram": {"value": 28656, "unit": "MB"}}}],
    "process": [{"gpu": 0, "process_list": [{"process_info": {
        "name": "python3", "pid": ME,
        "memory_usage": {"vram_mem": {"value": 20401094656, "unit": "B"}}}}]}],
}


def _dev() -> Device:
    return Device(id="d-mi", name="mi", kind=Kind.PERMANENT)


@posix
def test_rocm_smi_cards_and_their_jobs_are_read(tmp_path):
    bin_ = _bin(tmp_path)
    _tool(bin_, "rocm-smi", ROCM_SMI)
    out = probe(make_home(tmp_path), PATH=f"{bin_}:{os.environ['PATH']}")
    snap = parse_payload(out).to_dict()
    assert snap["gpu_vendor"] == "amd" and snap["gpu_present"] == "1"
    g0, g1 = snap["gpus"]
    assert (g0["name"], g0["uuid"]) == ("AMD Instinct MI210", "0x18f68e602b8a790f")
    assert g0["vram_total_mib"] == 65520 and g0["vram_used_mib"] == 16384
    assert g0["vram_free_mib"] == 65520 - 16384
    assert (g0["util_pct"], g0["temp_c"], g0["power_w"]) == (87, 51, 212.0)
    assert g1["util_pct"] == 0
    view = device_view(_dev(), {"status": "ok"}, snap)
    jobs = view["gpus"][0]["jobs"]
    assert [j["pid"] for j in jobs] == [ME] and jobs[0]["vram_mib"] == 16384
    assert view["gpus"][1]["jobs"] == []


@posix
def test_amd_smi_is_read_too_with_its_units(tmp_path):
    bin_ = _bin(tmp_path)
    _tool(bin_, "amd-smi", AMD_SMI)
    snap = parse_payload(probe(make_home(tmp_path),
                               PATH=f"{bin_}:{os.environ['PATH']}")).to_dict()
    (g,) = snap["gpus"]
    assert g["name"] == "AMD Radeon PRO W7900" and g["uuid"] == "0xA1B2C3D4E5F60718"
    assert (g["vram_total_mib"], g["vram_used_mib"], g["vram_free_mib"]) == (49136, 20480, 28656)
    assert (g["util_pct"], g["temp_c"], g["power_w"]) == (64, 58, 188.0)
    (job,) = device_view(_dev(), {"status": "ok"}, snap)["gpus"][0]["jobs"]
    assert job["pid"] == ME and job["vram_mib"] == 19456


@posix
def test_a_shared_machine_lists_no_ones_amd_jobs(tmp_path):
    bin_ = _bin(tmp_path)
    _tool(bin_, "rocm-smi", ROCM_SMI)
    out = probe(make_home(tmp_path), PATH=f"{bin_}:{os.environ['PATH']}", FLEET_MODE="shared")
    assert "#GPUPROC" not in out and len(parse_payload(out).gpus) == 2


@posix
def test_an_amd_tool_that_reads_no_gpu_is_an_alert_not_a_crash(tmp_path):
    bin_ = _bin(tmp_path)
    _tool(bin_, "rocm-smi", {})
    snap = parse_payload(probe(make_home(tmp_path),
                               PATH=f"{bin_}:{os.environ['PATH']}")).to_dict()
    assert snap["gpu_present"] == "err" and snap["gpus"] == []
    assert any("amdgpu driver" in a for a in device_view(_dev(), {"status": "ok"}, snap)["alerts"])
    assert "gpu" in facts(_dev(), snap, None) and "rocm" not in facts(_dev(), snap, None)


def test_an_amd_card_is_rocm_not_cuda():
    snap = {"gpu_vendor": "amd", "gpu_present": "1",
            "gpus": [{"vram_total_mib": 65520, "vram_used_mib": 0, "vram_free_mib": 65520}]}
    f = facts(_dev(), snap, None)
    assert "rocm" in f and "cuda" not in f and "vram-64g" in f


def test_an_older_probe_without_a_vendor_is_nvidia():
    snap = {"gpu_present": "1",
            "gpus": [{"vram_total_mib": 24564, "vram_used_mib": 0, "vram_free_mib": 24564}]}
    assert "cuda" in facts(_dev(), snap, None)
