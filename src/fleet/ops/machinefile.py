"""Read and change a machine's own machine.yaml, on that machine.

The file lives on the machine it describes (see notes.py), so changing it means going
there: read it, make the change to what was read, and write it back only if nobody
changed it in between -- the file is the machine's, and two people editing it from two
places must not erase each other. A write that finds the file changed reads it again and
makes the change again, a few times, then says so.

The user's file is the one written: it needs no root. `/etc/fleet/machine.yaml`, the
machine-wide one, is read by every probe but changed only by hand.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess

from .. import notes
from ..ssh.cmd import (WINDOWS, Endpoint, build_argv, local_platform, local_shell_argv,
                       remote_platform, run as sshrun)

DONE = "fleet-machinefile-done"

# Where the user's file is. The probe reads the first of these that exists on macOS, so
# writing the first is right everywhere: ~/.config/fleet is read before Library there.
_POSIX = '"$HOME/.config/fleet/machine.yaml"'
_PS = "(Join-Path $env:LOCALAPPDATA 'fleet\\machine.yaml')"


class MachineFileError(RuntimeError):
    pass


def _read_script(platform: str) -> str:
    if platform == WINDOWS:
        return (f"$f={_PS}\n"
                "if(Test-Path -LiteralPath $f){[Convert]::ToBase64String("
                "[IO.File]::ReadAllBytes($f))}\n"
                f"'{DONE}'\n")
    return (f'f={_POSIX}; [ -f "$f" ] && base64 < "$f" | tr -d "\\n"; echo; echo {DONE}')


def _write_script(platform: str, old: bytes, new: str) -> str:
    """Replace the file with `new` if it still holds `old`; print CHANGED if it does not.

    Compared as bytes, both ends: a file saved with a BOM or CRLF would otherwise never
    match the text it decodes to, and every write would read as a change by somebody else.

    The check and the replace happen under a lock. Without one, two edits at once both
    found the file as they had read it, both replaced it, and one was lost while each
    printed its ✓: eight `fleet edit --tag` run together kept four tags. A lock left by a
    write that was killed is taken over after a minute.
    """
    old_sha = hashlib.sha256(old).hexdigest()
    b64 = base64.b64encode(new.encode()).decode()
    if platform == WINDOWS:
        return (f"$f={_PS}\n"
                # One mutex per user session: the file is in that user's own folder.
                "$m=New-Object Threading.Mutex($false,'Local\\fleet-machine-yaml')\n"
                "try{$own=$m.WaitOne(20000)}catch [Threading.AbandonedMutexException]{$own=$true}\n"
                # No try/finally: `-Command -` runs one line at a time, and a block
                # cannot span lines. A write that dies leaves the mutex abandoned, which
                # the next WaitOne takes over.
                "if(-not $own){'CHANGED'; exit 0}\n"
                "$cur=[byte[]]@(); if(Test-Path -LiteralPath $f){$cur=[IO.File]::ReadAllBytes($f)}\n"
                "$h=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create()"
                ".ComputeHash($cur)).Replace('-','').ToLower()\n"
                f"if($h -ne '{old_sha}'){{'CHANGED'; $m.ReleaseMutex(); exit 0}}\n"
                # One line, so that one try can hold it: a folder it may not write
                # failed each step on its own line, and the last one still said done.
                "$t=\"$f.$PID\"\n"
                "try{New-Item -ItemType Directory -Force -ErrorAction Stop "
                "-Path (Split-Path $f) | Out-Null; "
                f"[IO.File]::WriteAllBytes($t,[Convert]::FromBase64String('{b64}')); "
                "Move-Item -Force -ErrorAction Stop -LiteralPath $t -Destination $f; "
                f"$m.ReleaseMutex(); '{DONE}'}}"
                "catch{Remove-Item -LiteralPath $t -ErrorAction SilentlyContinue; "
                "$m.ReleaseMutex(); "
                "[Console]::Error.WriteLine($_.Exception.Message); exit 1}\n")
    # sha256sum on Linux and busybox, shasum on macOS; the comparison is the same.
    # mkdir is the one atomic test-and-set every POSIX shell has (no flock on macOS).
    #
    # A folder it may not write -- a read-only home, a ~/.config/fleet that `sudo` made
    # root's -- said so at once. The lock's mkdir failed for that reason too, and was
    # taken for somebody else's lock: twenty seconds of waiting, ten times over, and then
    # "the file kept changing", which sent the user looking for another writer.
    return (f'f={_POSIX}; l="$f.lock"; d=$(dirname "$f"); mkdir -p "$d" || exit 1; '
            '[ -w "$d" ] || { echo "cannot write $d" >&2; exit 1; }; n=0; '
            'until mkdir "$l" 2>/dev/null; do '
            '[ -n "$(find "$l" -prune -mmin +1 2>/dev/null)" ] && rmdir "$l" 2>/dev/null; '
            'n=$((n+1)); [ $n -ge 100 ] && { echo CHANGED; exit 0; }; '
            'sleep 0.2 2>/dev/null || sleep 1; done; '
            "trap 'rmdir \"$l\" 2>/dev/null' EXIT; "
            'h=$( { [ -f "$f" ] && cat "$f"; } | { sha256sum 2>/dev/null || shasum -a 256; } '
            '| cut -d" " -f1); '
            f'[ "$h" = "{old_sha}" ] || {{ echo CHANGED; exit 0; }}; '
            # The decoder is picked before it is used: falling back to -D after a -d
            # that failed for want of space blamed "invalid option -- 'D'" and left
            # the half-written copy behind, on a disk already full.
            't="$f.$$"; b=-d; base64 -d </dev/null >/dev/null 2>&1 || b=-D; '
            f'if printf %s {b64} | base64 $b > "$t" && mv -f "$t" "$f"; then echo {DONE}; '
            'else rm -f "$t"; exit 1; fi')


def _run(ep: Endpoint | None, script: str, platform: str) -> str:
    if ep is None:                     # this machine: no ssh to talk to ourselves
        p = subprocess.run(local_shell_argv(), input=script.encode(), capture_output=True,
                           timeout=30)
    else:
        remote = ("powershell -NoProfile -Command -" if platform == WINDOWS else "sh -s")
        argv = build_argv(ep, remote=remote, multiplex=False, connect_timeout=10)
        try:
            p = sshrun(argv, input=script.encode(), timeout=30)
        except subprocess.TimeoutExpired:
            raise MachineFileError("timed out") from None
    out = (p.stdout or b"").decode(errors="replace")
    if DONE not in out and "CHANGED" not in out:
        detail = (p.stderr or b"").decode(errors="replace").strip()[-200:]
        raise MachineFileError(detail or f"exit status {p.returncode}")
    return out


def read_bytes(ep: Endpoint | None, platform: str) -> bytes:
    out = _run(ep, _read_script(platform), platform)
    b64 = out.split(DONE)[0].strip().splitlines()
    try:
        return base64.b64decode(b64[-1]) if b64 else b""
    except ValueError as e:
        raise MachineFileError(f"could not read it: {e}") from None


def read(ep: Endpoint | None, platform: str) -> str:
    return read_bytes(ep, platform).decode("utf-8-sig", errors="replace")


def change(ep: Endpoint | None, platform: str, **edits) -> tuple[str, str]:
    """Make `edits` (see notes.edit) to the machine's file. Returns (before, after)."""
    for _ in range(10):
        raw = read_bytes(ep, platform)
        old = raw.decode("utf-8-sig", errors="replace")
        new = notes.edit(old, **edits)
        # Nothing to say on a machine with no file: creating one that holds only the
        # header (`--untag x` where x never was) is litter on someone's machine.
        if new == old or (not old.strip() and not notes.parse(new)):
            return old, old
        if "CHANGED" not in _run(ep, _write_script(platform, raw, new), platform):
            return old, new
    raise MachineFileError("the file kept changing while we wrote it; try again")


def platform_of(snap: dict | None, *, here: bool) -> str:
    return local_platform() if here else remote_platform(snap)
