"""`fleet access --prune`: keys fleet put on machines and then forgot.

Found on a real fleet rebuilt from scratch: every host still held the old fleet's keys,
and the only way to see them was to fetch each authorized_keys and compare fingerprints
by hand. fleet labels every block it writes, so it can do that comparison itself.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from fleet.ops import prune
from fleet.ssh.authkeys import block
from fleet.state import access as acl

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEfO2BAfm3M8YGY39abf7HwQ9kNkKOR7nesP3z7/PatB k"


def _acc():
    acc = acl.Access(fleet_id="aaaaaa", center="SHA256:center")
    acc.keys = {"SHA256:center": {"name": "hub", "device_id": "id:hub"},
                "SHA256:box": {"name": "box", "device_id": "id:box"},
                "SHA256:lap": {"name": "laptop", "device_id": "id:lap"}}
    acc.allow = [acl.Edge(src="SHA256:lap", dst="SHA256:box", user="root")]
    return acc


def _markers(*blocks):
    return "\n".join(f"# fleet:{fid}:begin from={src} user={user}" for fid, src, user in blocks)


def test_what_the_list_still_wants_is_left_alone():
    acc = _acc()
    text = _markers(("aaaaaa", "SHA256:center", "root"), ("aaaaaa", "SHA256:lap", "root"))
    assert prune.judge(acc, "box", "root", {"SHA256:box"}, prune.parse(text)[0], set()) == []


def test_a_key_the_list_forgot_is_removed():
    acc = _acc()
    text = _markers(("aaaaaa", "SHA256:center", "root"), ("aaaaaa", "SHA256:old-lap", "root"))
    found = prune.judge(acc, "box", "root", {"SHA256:box"}, prune.parse(text)[0], set())
    assert [(f.src, f.action) for f in found] == [("SHA256:old-lap", prune.REMOVE)]
    assert "no longer in the list" in found[0].label
    # laptop may reach box as root; the same key in ubuntu's file there is not wanted.
    text = _markers(("aaaaaa", "SHA256:lap", "ubuntu"))
    found = prune.judge(acc, "box", "ubuntu", {"SHA256:box"}, prune.parse(text)[0], set())
    assert [(f.label, f.action) for f in found] == [("laptop", prune.REMOVE)]


def test_another_fleets_blocks_are_kept_unless_that_fleet_is_named():
    acc = _acc()
    text = _markers(("aaaaaa", "SHA256:center", "root"), ("bbbbbb", "SHA256:x", "root"))
    found = prune.judge(acc, "box", "root", {"SHA256:box"}, prune.parse(text)[0], set())
    assert [f.action for f in found] == [prune.OTHER_FLEET]
    found = prune.judge(acc, "box", "root", {"SHA256:box"}, prune.parse(text)[0], {"bbbbbb"})
    assert [f.action for f in found] == [prune.REMOVE]


def test_the_key_the_center_logs_in_with_is_never_taken():
    """An old fleet's block holding the center's own key may be the only way in."""
    acc = _acc()
    text = _markers(("bbbbbb", "SHA256:center", "root"))
    found = prune.judge(acc, "box", "root", {"SHA256:box"}, prune.parse(text)[0], {"bbbbbb"})
    assert [f.action for f in found] == [prune.LAST_COPY]
    # With this fleet's own block for the center beside it, the old one can go.
    text = _markers(("bbbbbb", "SHA256:center", "root"), ("aaaaaa", "SHA256:center", "root"))
    found = prune.judge(acc, "box", "root", {"SHA256:box"}, prune.parse(text)[0], {"bbbbbb"})
    assert [f.action for f in found] == [prune.REMOVE]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_the_scan_reads_only_markers_and_says_it_finished(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text(
        KEY + " me\n" + block("aaaaaa", "SHA256:old", "root", KEY) + "\n"
        + block("bbbbbb", "SHA256:x", "root", KEY) + "\n")
    p = subprocess.run(["sh", "-c", prune.list_command("posix")], capture_output=True,
                       text=True, env={**os.environ, "HOME": str(tmp_path)})
    assert "fleet-scan-done" in p.stdout
    blocks, bare = prune.parse(p.stdout)
    assert blocks == [("aaaaaa", "SHA256:old", "root"), ("bbbbbb", "SHA256:x", "root")]
    assert bare == [acl.fingerprint(KEY)]          # the user's own line, outside a block


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_a_machine_with_no_file_reads_as_empty_not_as_unreachable(tmp_path):
    p = subprocess.run(["sh", "-c", prune.list_command("posix")], capture_output=True,
                       text=True, env={**os.environ, "HOME": str(tmp_path)})
    assert "fleet-scan-done" in p.stdout and prune.parse(p.stdout) == ([], [])


def test_a_key_in_three_forms_keeps_the_copy_fleet_can_manage():
    """Found on a real NAS: the center's key bare, in an old fleet's block and in this
    fleet's. Only this fleet's block is one `--deny` can find; that is the one to keep,
    and the bare line is never fleet's to touch -- but it is said."""
    acc = _acc()
    text = (KEY + " bare\n"
            + block("bbbbbb", "SHA256:center", "root", KEY) + "\n"
            + block("aaaaaa", "SHA256:center", "root", KEY) + "\n")
    acc.keys[acl.fingerprint(KEY)] = {"name": "hub-again", "device_id": "id:x"}
    blocks, bare = prune.parse(text)
    found = prune.judge(acc, "nas", "root", set(), blocks, {"bbbbbb"}, bare)
    assert [(f.fleet_id, f.action) for f in found] == [("bbbbbb", prune.REMOVE),
                                                       ("", prune.BARE)]


def test_a_wanted_key_whose_own_block_is_not_written_yet_keeps_its_old_copy():
    acc = _acc()                         # laptop may reach box; its block is not there yet
    text = _markers(("aaaaaa", "SHA256:center", "root"), ("bbbbbb", "SHA256:lap", "root"))
    found = prune.judge(acc, "box", "root", {"SHA256:box"}, prune.parse(text)[0], {"bbbbbb"})
    assert [f.action for f in found] == [prune.LAST_COPY]
