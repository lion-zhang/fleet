"""Being a member: the view from a machine that is in a fleet and does not decide.

A member holds no access list -- only the center does -- so every command that started
by loading one refused on a member with "not a center -- start one with `fleet center
--init`". That was wrong three ways, all found on a real fleet: `fleet center --json`,
which agents are told to read `is_center` from, could not answer `false`; `fleet center
--leave`, a member's own right, could not run at all; and the advice would have started
a second, competing fleet on a machine already in one.

What a member does hold is enough: the center's key, pinned; where it listens; when it
was last heard from; which fleet this is, from the envelopes the center signs; and its
own authorized_keys, where every block the fleet placed is labelled with that fleet's id.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ..state import access as acl
from ..state import inventory as inv
from ..ssh.authkeys import sync_command
from ..ssh.cmd import WINDOWS, local_platform, local_shell_argv


def center_device():
    """The center's record, matched by the key this machine pinned.

    By key first: `role` rides the inventory merge and is a label, while the pinned key
    is the thing this machine actually trusts. The label is the fallback for a record
    published before keys were.
    """
    devices = inv.live(inv.load())
    pinned = acl.trusted_center_pubkey()
    if pinned:
        try:
            want = acl.fingerprint(pinned)
        except acl.AccessError:
            want = ""
        for d in devices:
            try:
                if d.pubkey and acl.fingerprint(d.pubkey) == want:
                    return d
            except acl.AccessError:
                continue
    return next((d for d in devices if d.role == "center"), None)


def center_name() -> str:
    dev = center_device()
    return dev.name if dev else "the center"


def status() -> dict:
    seen = acl.center_last_seen()
    return {"is_center": False, "member": True,
            "fleet_id": fleet_id(),
            "center": center_name(),
            "center_url": acl.center_url(),
            "last_seen_s": (int(time.time()) - seen) if seen else None}


# ------------------------------------------------------------------ the keys on disk

def _authorized_keys_files() -> list[Path]:
    """Where this machine's sshd reads keys for the user running fleet.

    Windows has two, and which one applies depends on group membership sshd decides --
    so both are read and whichever holds fleet blocks is the one that matters.
    """
    if local_platform() == WINDOWS:
        import os

        files = [Path.home() / ".ssh" / "authorized_keys"]
        if program_data := os.environ.get("ProgramData"):
            files.append(Path(program_data) / "ssh" / "administrators_authorized_keys")
        return files
    return [Path.home() / ".ssh" / "authorized_keys"]


_BEGIN = re.compile(r"^# fleet:([0-9a-zA-Z_-]+):begin from=(\S+)")


def blocks_on_disk() -> list[tuple[str, str]]:
    """Every (fleet_id, from) block in this user's authorized_keys files."""
    found = []
    for path in _authorized_keys_files():
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if m := _BEGIN.match(line.strip()):
                found.append((m.group(1), m.group(2)))
    return found


def fleet_id() -> str:
    """Which fleet this is: as the center last said, or read off our own key blocks.

    The blocks are the fallback for a machine enrolled before members recorded the id:
    the center's own block on this machine is labelled with it, and the center's key is
    the one pinned.
    """
    if known := acl.member_fleet_id():
        return known
    pinned = acl.trusted_center_pubkey()
    try:
        center_fp = acl.fingerprint(pinned) if pinned else ""
    except acl.AccessError:
        center_fp = ""
    ids = {fid for fid, src in blocks_on_disk() if src == center_fp}
    return ids.pop() if len(ids) == 1 else ""


def leave() -> tuple[str, int]:
    """Take this machine out of its fleet. Returns (fleet_id, blocks removed).

    Every block the fleet placed here comes off -- the center's, and every peer's that
    was granted this machine -- found by their labels rather than by a list this machine
    does not hold. Then the pin and the center's address are forgotten, so the machine
    stops trusting that center and stops asking it for anything.

    Needs nobody's permission: you own the machine you are standing on. The center will
    see it as unreachable until told, which is the same as before.
    """
    fid = fleet_id()
    sources = sorted({src for f, src in blocks_on_disk() if f == fid}) if fid else []
    shell = local_shell_argv()
    for src in sources:
        script = sync_command(fid, src, pubkey=None, platform=local_platform())
        subprocess.run(shell, input=script.encode(), capture_output=True)
    for path in (acl.CACHE_PATH, acl.OUTBOX_PATH, acl.INBOX_PATH):
        path.unlink(missing_ok=True)
    return fid, len(sources)
