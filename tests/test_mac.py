"""A Mac as fleet sees it: the probe run with stand-ins for the tools a Mac answers with."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from fleet import cli
from fleet.models import Device, Kind
from fleet.probe.parse import parse_payload
from fleet.render.view import Detail, device_view
from test_resources import make_home, probe

posix = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                           reason="runs payload.sh")

# What `df -Pk` says on a 1 TB Mac whose data volume has filled it: every APFS volume
# reports the whole disk's size and free space, and only its own use.
APFS = """Filesystem     1024-blocks      Used Available Capacity  Mounted on
/dev/disk3s1s1   971350180  11384920  31205400    27%    /
devfs                  204       204         0   100%    /dev
/dev/disk3s5     971350180 918303344  31205400    97%    /System/Volumes/Data
/dev/disk5s1     488245288 401234000  87011288    83%    /Volumes/T7 Shield
"""

# `ps -axo pid=,ppid=,user=,pcpu=,rss=,etime=,comm=` on a quiet Mac.
BSD_PS = """    1     0 root               0.0  14256 37-16:44:01 /sbin/launchd
  401     1 _windowserver      3.1 180800 37-16:43:12 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer
 1203     1 lin                0.4  92100 37-16:40:02 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
  777     1 lin                0.0   2100       00:04 /bin/zsh
"""


def _mac(tmp_path) -> dict:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "uname").write_text(
        '#!/bin/sh\ncase "$1" in -m) echo arm64 ;; -r) echo 24.6.0 ;; *) echo Darwin ;; esac\n')
    (bin_ / "df").write_text("#!/bin/sh\ncat <<'X'\n" + APFS + "X\n")
    (bin_ / "ps").write_text("#!/bin/sh\ncat <<'X'\n" + BSD_PS + "X\n")
    for tool in bin_.iterdir():
        tool.chmod(0o755)
    snap = parse_payload(probe(make_home(tmp_path), PATH=f"{bin_}:{os.environ['PATH']}"))
    dev = Device(id="darwin:hwuuid:x", name="mac", kind=Kind.PERMANENT,
                 endpoints=[{"target": "mac.example.ts.net", "user": "lin", "port": 22}])
    state = {"status": "ok", "last_probe_at": snap.ts, "last_ok_at": snap.ts}
    return device_view(dev, state, snap.to_dict(), Detail.FULL)


@posix
def test_a_mac_whose_disk_is_full_says_so(tmp_path):
    # "/" read 11G used against 30G free: 27% "used" on a disk 97% full, and the alert
    # that a long job will die there could not come.
    row = _mac(tmp_path)
    disks = {d["mount"]: d for d in row["disks"]}
    assert disks["/"]["use_pct"] == 97 and disks["/"]["free_gb"] == 29.8
    assert disks["/Volumes/T7 Shield"]["use_pct"] == 82
    assert any(a.startswith("disk /: 97% full") for a in row["alerts"])


@posix
def test_a_mac_lists_only_processes_that_use_the_cpu(tmp_path):
    # Every idle process came too: launchd and a shell at 0% as what kept it busy.
    row = _mac(tmp_path)
    assert [p["comm"] for p in row["top_cpu"]] == ["WindowServer"]


def test_a_long_user_name_keeps_the_process_list_in_line(capsys):
    procs = [{"pid": 401, "user": "_windowserver", "cpu_pct": 3.1, "comm": "WindowServer",
              "scope": "cpu", "klass": "other"},
             {"pid": 1203, "user": "lin", "cpu_pct": 2.4, "comm": "Google Chrome",
              "scope": "cpu", "klass": "other"}]
    dev = Device(id="darwin:hwuuid:x", name="mac", kind=Kind.PERMANENT,
                 endpoints=[{"target": "mac.example.ts.net", "user": "lin", "port": 22}])
    cli._print_show(device_view(dev, {"status": "ok"}, {"processes": procs}, Detail.FULL))
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "%" in ln]
    assert len(lines) == 2 and len({ln.index("%") for ln in lines}) == 1, lines
