"""Parser tests run entirely against real captured probe output. No network."""

from __future__ import annotations

import pathlib

import pytest

from fleet.models import ProcClass
from fleet.probe.parse import MissingSentinel, classify, parse_etime, parse_payload

FIX = pathlib.Path(__file__).parent / "fixtures" / "probe"


def load(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


# --------------------------------------------------------------- sentinel contract
def test_missing_sentinel_is_an_error():
    """A probe without #END was truncated or killed; it must never parse as success."""
    with pytest.raises(MissingSentinel):
        parse_payload("#FLEET v1\nhost.hostname=x\ncpu.cores=8\n")


def test_truncated_mid_section_still_raises():
    body = load("gpu-box").split("#END")[0]
    with pytest.raises(MissingSentinel):
        parse_payload(body)


# --------------------------------------------------------------- the 4090 box
def test_lin_xps_basic_facts():
    s = parse_payload(load("gpu-box"))
    assert s.hostname == "gpu-box"
    assert s.machine_id == "11111111111111111111111111111111"
    assert s.cpu_cores == 32
    assert s.arch == "x86_64"
    assert s.mem_total_kb == 29906208
    assert s.gpu_present == "1"
    assert s.gpu_driver == "595.84"
    assert s.load == (0.39, 0.11, 0.08)


def test_lin_xps_disks_include_workspace():
    s = parse_payload(load("gpu-box"))
    mounts = {d.mount for d in s.disks}
    assert {"/", "/home", "/workspace"} <= mounts


def test_lin_xps_gpu_is_idle_despite_held_vram():
    """The core correctness case: a desktop session holds VRAM, but the box is FREE.
    A naive 'vram_used > 0 => busy' rule would wrongly reject this GPU."""
    s = parse_payload(load("gpu-box"))
    (gpu,) = s.gpus
    assert gpu.name == "NVIDIA GeForce RTX 4090"
    assert gpu.vram_total_mib == 24564
    assert gpu.vram_used_mib == 853          # non-zero...
    assert gpu.vram_free_mib == 23197        # ...yet ~23 GB is free
    assert gpu.util_pct == 0
    assert gpu.compute_used_mib == 0         # nothing is actually computing
    assert gpu.display_used_mib == 439       # msedge + ptyxis + nautilus + code
    assert gpu.busy is False


def test_lin_xps_unattributed_vram_is_reported():
    """853 used - 439 attributed = 414 MiB held by processes we cannot see.
    This must be surfaced, never silently counted as free."""
    s = parse_payload(load("gpu-box"))
    (gpu,) = s.gpus
    assert gpu.unattributed_mib == 414


def test_lin_xps_desktop_apps_are_not_jobs():
    s = parse_payload(load("gpu-box"))
    gpu_procs = {p.comm: p for p in s.processes if p.scope == "gpu"}
    assert set(gpu_procs) == {"msedge", "ptyxis", "nautilus", "code"}
    assert all(p.klass is ProcClass.DISPLAY for p in gpu_procs.values())
    assert gpu_procs["msedge"].vram_mib == 156
    assert gpu_procs["msedge"].user == "user"


# --------------------------------------------------------------- no-GPU hosts
@pytest.mark.parametrize("name,cores,arch", [("vm-a", 2, "aarch64"), ("vm-b", 2, "x86_64")])
def test_gpuless_linux_hosts_parse_cleanly(name, cores, arch):
    """No nvidia-smi is a normal outcome, not an error."""
    s = parse_payload(load(name))
    assert s.gpu_present == "0"
    assert s.gpus == []
    assert s.gpu_error == ""
    assert s.cpu_cores == cores
    assert s.arch == arch


def test_macos_host_parses_and_finds_local_llm():
    s = parse_payload(load("macos-laptop"))
    assert s.os.startswith("macOS")
    assert s.machine_id == "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    assert s.cpu_model == "Apple M3 Max"
    assert s.cpu_cores == 16
    assert s.gpu_present == "0"          # no nvidia-smi; unified memory not measured
    # the MLX server on :8010 must be discovered
    assert 8010 in {svc.port for svc in s.services}


def test_macos_uptime_is_an_uptime_not_an_epoch():
    """Regression: a greedy sed matched 'usec' in kern.boottime and yielded ~now."""
    s = parse_payload(load("macos-laptop"))
    assert s.uptime_s is not None
    assert 0 < s.uptime_s < 5 * 365 * 86400


# --------------------------------------------------------------- service filtering
def test_ssh_and_dns_are_not_reported_as_services():
    """Port 22/53/631 are OS plumbing; surfacing them as 'services' is noise."""
    for name in ("gpu-box", "vm-a", "vm-b"):
        ports = {svc.port for svc in parse_payload(load(name)).services}
        assert not ({22, 53, 111, 631} & ports), f"{name} leaked a boring port"


def test_interface_scope_is_stripped_from_address():
    """ss reports '127.0.0.53%lo'; the '%lo' must not survive into a usable address."""
    for name in ("gpu-box", "vm-a"):
        for svc in parse_payload(load(name)).services:
            assert "%" not in svc.addr


# --------------------------------------------------------------- unit helpers
@pytest.mark.parametrize("raw,secs", [
    ("690348", 690348),          # linux etimes
    ("41:47", 2507),             # macOS MM:SS
    ("03:25:24", 12324),         # macOS HH:MM:SS
    ("12-05:45:37", 1057537),    # macOS DD-HH:MM:SS
    ("", 0),
    ("garbage", 0),
])
def test_parse_etime_handles_both_linux_and_macos(raw, secs):
    assert parse_etime(raw) == secs


def test_classify_unknown_process_is_compute():
    """Unknown means 'assume it is real work' -- the safe default."""
    assert classify("train_sft.py", 20000) is ProcClass.COMPUTE
    assert classify("python", 8000) is ProcClass.COMPUTE
    assert classify("msedge", 156) is ProcClass.DISPLAY
    assert classify("/usr/lib/foo/gnome-shell", 90) is ProcClass.DISPLAY


def test_a_windows_host_says_it_is_windows():
    """cmd.exe answering our `sh -s`. The host is up and the key worked -- it simply has
    no POSIX shell. Untreated, the failure surfaces as the tail of a Windows error
    ("operable program or batch file.") which says nothing about what to do about it."""
    from fleet.probe.runner import classify_stderr

    status, detail = classify_stderr(
        "'sh' is not recognized as an internal or external command,\n"
        "operable program or batch file.\n")
    assert "Windows" in detail
    assert "POSIX shell" in detail


def test_a_windows_host_is_not_mistaken_for_an_auth_failure():
    """It must not read as 'rejected our key' -- the key worked, and offering to install
    another would be advice that cannot help."""
    from fleet.models import Status
    from fleet.probe.runner import classify_stderr

    status, _ = classify_stderr("operable program or batch file.\n")
    assert status is not Status.AUTH_FAILED
