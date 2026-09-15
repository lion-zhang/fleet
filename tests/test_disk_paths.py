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
    from fleet import cli
    from fleet.state import inventory as inv, store
    from fleet.ops import rows
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

    monkeypatch.setattr(rows, "probe_many", fake_probe_many)
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


# --------------------------------------------------------------- read-only volumes

@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_the_probe_reports_whether_a_disk_can_be_written_to(tmp_path):
    """A mounted DMG is 100% full by definition and read-only, so it alerted forever.
    Knowing it is read-only is what lets the alert skip it while `fleet show` still
    lists it honestly."""
    ro = tmp_path / "readonly"
    ro.mkdir()
    ro.chmod(0o500)
    rows = _disk_rows(_run_payload(FLEET_DISK_PATHS=f"{tmp_path} {ro}"))
    by_mount = {r.split("|")[0]: r.split("|") for r in rows}
    assert by_mount[str(ro)][4] == "0", "read-only volume should report rw=0"
    assert by_mount[str(tmp_path)][4] == "1", "writable volume should report rw=1"


def test_an_older_snapshot_without_the_field_is_assumed_writable():
    """Every captured fixture predates this field, and treating them as read-only
    would silently stop alerting on disks that really can fill up."""
    from fleet.probe.parse import parse_payload

    text = ("#HOST\nhost.machine_id=11111111111111111111111111111111\n"
            "#DISK mount|total_kb|used_kb|avail_kb\n"
            "/|100|50|50\n#END rc=0\n")
    assert parse_payload(text).disks[0].writable is True


def test_a_full_read_only_volume_raises_no_alert():
    from fleet.models import Device, Kind
    from fleet.render.view import Detail, device_view

    snap = {"disks": [{"mount": "/Volumes/App", "total_kb": 100, "used_kb": 100,
                       "avail_kb": 0, "writable": False}]}
    v = device_view(Device(id="x", name="box", kind=Kind.PERMANENT),
                    {"status": "ok", "last_probe_at": 1, "last_ok_at": 1},
                    snap, Detail.COMPACT)
    assert not any("disk" in a.lower() for a in v["alerts"]), v["alerts"]


def test_a_full_writable_volume_still_raises_one():
    """The filter must not silence the disks that actually matter."""
    from fleet.models import Device, Kind
    from fleet.render.view import Detail, device_view

    snap = {"disks": [{"mount": "/workspace", "total_kb": 100, "used_kb": 96,
                       "avail_kb": 4, "writable": True}]}
    v = device_view(Device(id="x", name="box", kind=Kind.PERMANENT),
                    {"status": "ok", "last_probe_at": 1, "last_ok_at": 1},
                    snap, Detail.COMPACT)
    assert any("/workspace" in a for a in v["alerts"])


def test_list_views_carry_every_mount_not_just_the_roomiest():
    """Reporting only the roomiest mount hid a rental's nearly-full / behind its big
    /workspace -- and the mount that ends a long job is precisely the hidden one.

    Trimmed to mount+free at COMPACT, the way `gpus` is, so `fleet ls --json` stays
    small for the agents that read it.
    """
    from fleet.models import Device, Kind
    from fleet.render.view import Detail, device_view

    gb = 1048576  # df reports kb, and free_gb rounds -- toy numbers all collapse to 0.0
    snap = {"disks": [{"mount": "/", "total_kb": 40 * gb, "used_kb": 38 * gb,
                       "avail_kb": 2 * gb},
                      {"mount": "/workspace", "total_kb": 2200 * gb,
                       "used_kb": 100 * gb, "avail_kb": 2100 * gb}]}
    v = device_view(Device(id="x", name="box", kind=Kind.PERMANENT),
                    {"status": "ok", "last_probe_at": 1, "last_ok_at": 1},
                    snap, Detail.COMPACT)
    assert [d["mount"] for d in v["disks"]] == ["/", "/workspace"]
    assert set(v["disks"][0]) == {"mount", "free_gb"}
    # the reduced figures stay exactly as they were: this is additive
    assert v["disk_mount"] == "/workspace"


def test_a_read_only_volume_is_still_listed_in_full_detail():
    """Skipping the alert is not the same as hiding the disk."""
    from fleet.models import Device, Kind
    from fleet.render.view import Detail, device_view

    snap = {"disks": [{"mount": "/Volumes/App", "total_kb": 100, "used_kb": 100,
                       "avail_kb": 0, "writable": False}]}
    v = device_view(Device(id="x", name="box", kind=Kind.PERMANENT),
                    {"status": "ok", "last_probe_at": 1, "last_ok_at": 1},
                    snap, Detail.FULL)
    assert [d["mount"] for d in v["disks"]] == ["/Volumes/App"]
    assert v["disks"][0]["writable"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_a_disk_row_from_the_current_payload_actually_parses(tmp_path):
    """The parser required exactly four fields, so adding the rw column silently
    dropped every disk. Asserting on raw rows missed it -- this goes through the
    parser the way the probe does."""
    from fleet.probe.parse import parse_payload

    snap = parse_payload(_run_payload(FLEET_DISK_PATHS=str(tmp_path)))
    assert [d.mount for d in snap.disks] == [str(tmp_path)]
    assert snap.disks[0].writable is True


@pytest.mark.skipif(sys.platform == "win32", reason="payload is POSIX sh")
def test_root_is_always_treated_as_writable(tmp_path):
    """On macOS / is the read-only signed system volume, so `[ -w / ]` is false -- but
    the free space df reports for it is the shared APFS container that user data fills.
    Trusting -w there would silence the most important alert on every Mac."""
    from fleet.probe.parse import parse_payload

    snap = parse_payload(_run_payload(FLEET_DISK_PATHS="/"))
    assert snap.disks[0].mount == "/"
    assert snap.disks[0].writable is True


def test_the_probe_payload_has_no_carriage_returns():
    """It is piped verbatim into `sh` on the far side, so a CR is part of the command.
    Git for Windows defaults to core.autocrlf=true and rewrote this file on checkout;
    every probe from the Windows center then failed with `sh: 8: \\r: not found` and
    `Syntax error: "|" unexpected`, while the same fleet looked healthy from macOS.

    Checked on the bytes the code will actually send, not on what is committed -- the
    conversion happens at checkout, so only the file on disk can show it."""
    from fleet.probe.runner import PAYLOAD

    raw = PAYLOAD.read_bytes()
    assert b"\r" not in raw, (
        f"{PAYLOAD} has {raw.count(chr(13).encode())} carriage return(s); "
        "the .gitattributes entry pinning it to LF is missing or not applied "
        "(try `git add --renormalize .`)")


def test_gitattributes_pins_the_payload_line_endings():
    """The file above is only right on this machine if something pins it everywhere."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    attrs = (root / ".gitattributes")
    assert attrs.exists(), "nothing stops the next Windows clone reintroducing CRLF"
    text = attrs.read_text()
    assert "eol=lf" in text and ".sh" in text
