"""Per-device disk paths, from inventory through to the remote shell.

The default mount filter is a guess -- it whitelists /workspace, /data and friends
because that is where rental images usually put the big volume. When the guess is wrong
the user names the paths, and those names have to survive all the way into the awk
filter running on the far end of an SSH pipe.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from fleet.probe.runner import PAYLOAD, probe_env


# --------------------------------------------------------------- env plumbing

def test_probe_env_always_carries_the_mode():
    assert probe_env("shared", [])["FLEET_MODE"] == "shared"


def test_probe_env_passes_configured_disk_paths_space_separated():
    env = probe_env("full", ["/workspace", "/data"])
    assert env["FLEET_DISK_PATHS"] == "/workspace /data"


def test_probe_env_omits_disk_paths_when_the_device_has_none():
    """An absent variable means 'autodetect', which is not the same as an empty list."""
    assert "FLEET_DISK_PATHS" not in probe_env("full", [])


# --------------------------------------------------------------- the shell itself

def _disk_rows(stdout: str) -> list[str]:
    rows, inside = [], False
    for line in stdout.splitlines():
        if line.startswith("#DISK"):
            inside = True
            continue
        if inside:
            if line.startswith("#"):
                break
            if line.strip():
                rows.append(line)
    return rows


def _run_payload(**env) -> str:
    return subprocess.run(
        ["sh", str(PAYLOAD)], capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "FLEET_MODE": "full", **env},
    ).stdout


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_payload_reports_the_requested_path_even_when_it_is_not_a_mount_point(tmp_path):
    """`df /some/dir` reports the filesystem holding that directory. Matching mount
    names only would silently report nothing for a path that is not itself a mount."""
    rows = _disk_rows(_run_payload(FLEET_DISK_PATHS=str(tmp_path)))
    assert rows, "a real directory must produce a row"
    assert rows[0].split("|")[0] == str(tmp_path), "labelled with what was asked for"


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_payload_ignores_a_configured_path_that_does_not_exist():
    """A stale setting must not break the probe or emit a garbage row."""
    rows = _disk_rows(_run_payload(FLEET_DISK_PATHS="/definitely/not/here"))
    assert rows == []


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_payload_falls_back_to_autodetection_when_nothing_is_configured():
    rows = _disk_rows(_run_payload())
    assert any(r.split("|")[0] == "/" for r in rows), "root is always interesting"


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_configured_paths_replace_the_default_filter_rather_than_adding_to_it(tmp_path):
    """If the user names paths, those are the paths they want -- not those plus a
    guess. Otherwise the setting cannot be used to narrow a noisy machine."""
    rows = _disk_rows(_run_payload(FLEET_DISK_PATHS=str(tmp_path)))
    assert [r.split("|")[0] for r in rows] == [str(tmp_path)]


# --------------------------------------------------------------- the sweep

def test_devices_with_different_disk_paths_are_probed_in_separate_groups(tmp_path, monkeypatch):
    """The sweep batches devices to share one SSH fan-out. Batching by mode alone would
    hand one device's configured paths to every other device in the batch."""
    from fleet import cli, inventory as inv, store
    from fleet.models import Device, Kind

    devices = [
        Device(id="a", name="a", kind=Kind.PERMANENT, disk_paths=["/a"],
               endpoints=[{"target": "a.example", "user": "root", "port": 22}]),
        Device(id="b", name="b", kind=Kind.PERMANENT,
               endpoints=[{"target": "b.example", "user": "root", "port": 22}]),
    ]
    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")

    calls: list[tuple[frozenset, tuple]] = []

    def fake_probe_many(jobs, **kw):
        calls.append((frozenset(jobs), tuple(kw.get("disk_paths") or ())))
        return {}

    monkeypatch.setattr(cli, "probe_many", fake_probe_many)
    cli._rows(refresh=True)

    assert (frozenset({"a"}), ("/a",)) in calls
    assert (frozenset({"b"}), ()) in calls


# --------------------------------------------------------------- self-observation

def _cpuproc_rows(stdout: str) -> list[list[str]]:
    rows, inside = [], False
    for line in stdout.splitlines():
        if line.startswith("#CPUPROC"):
            inside = True
            continue
        if inside:
            if line.startswith("#"):
                break
            if line.strip():
                rows.append(line.split("|"))
    return rows


def _fake_ps(tmp_path):
    """A ps that reports itself the way the real one does, plus one genuine process.

    Using the real ps makes this test depend on whether the probe happens to land in
    the top ten by CPU, which is a coin flip -- a flaky test here is worse than none.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ps = bin_dir / "ps"
    # $$ and $PPID inside the fake are exactly what the real ps would report for itself
    ps.write_text(
        '#!/bin/sh\n'
        'printf "%s %s root 99.9 1000 0 ps\\n" "$$" "$PPID"\n'
        'printf "%s %s root 98.0 1000 0 sh\\n" "$PPID" "1"\n'
        'printf "4242 1 root 50.0 2000 9999 realwork\\n"\n')
    ps.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_the_probe_does_not_report_its_own_processes(tmp_path):
    """The probe runs ps, so ps sees itself -- and a freshly started process reports a
    huge pcpu, which put the probe's own shell at the top of `fleet top`. Measuring the
    measurement is the classic version of this bug."""
    bin_dir = _fake_ps(tmp_path)
    out = subprocess.run(
        ["sh", str(PAYLOAD)], capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "FLEET_MODE": "full"},
    ).stdout
    comms = [r[5].strip() for r in _cpuproc_rows(out) if len(r) > 5]
    assert "ps" not in comms, f"the probe reports its own ps: {comms}"
    assert "sh" not in comms, f"the probe reports its own shell: {comms}"


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_filtering_ourselves_out_keeps_everyone_else(tmp_path):
    """An over-broad filter that dropped everything would satisfy the test above while
    making the CPU list useless."""
    bin_dir = _fake_ps(tmp_path)
    out = subprocess.run(
        ["sh", str(PAYLOAD)], capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "FLEET_MODE": "full"},
    ).stdout
    assert "realwork" in [r[5].strip() for r in _cpuproc_rows(out) if len(r) > 5]


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_the_cpuproc_wire_format_is_unchanged():
    """ppid is used to filter and then dropped: the parser and every captured fixture
    still expect exactly six fields."""
    for row in _cpuproc_rows(_run_payload()):
        assert len(row) == 6, row
