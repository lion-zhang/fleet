"""Editing someone else's authorized_keys without destroying it.

This is the most dangerous code in fleet. The file it edits is the only thing standing
between you and a machine you may have no other way into, and a mistake is not a failed
command -- it is a box you cannot log into again, possibly for good.

Two rules follow, and everything here exists to keep them:

1. **Only ever touch lines inside a block we wrote.** The user's own key, the key a
   provider injected at boot, and another fleet's block must all come through untouched.
   Never rewrite the file wholesale from a desired state.
2. **Never leave the file empty or half-written.** Write a temp file beside it and
   rename, so a connection dropped mid-write leaves the old file intact.

Blocks are namespaced by fleet id as well as by grantee, so two fleets managing the same
machine cannot erase each other.

Windows needs its own everything -- different file, different permission model, no `sh`
-- so each command has a twin. The generators are pure functions over strings; running
them is the caller's problem, which is what makes the dangerous half testable without a
network.
"""

from __future__ import annotations

import shlex

# `fleet:` rather than something cuter: the person who finds this line in their
# authorized_keys months later needs to know what put it there.
_BEGIN = "# fleet:{fid}:begin from={src}"
_END = "# fleet:{fid}:end from={src}"


def begin_marker(fleet_id: str, from_id: str) -> str:
    return _BEGIN.format(fid=fleet_id, src=from_id)


def end_marker(fleet_id: str, from_id: str) -> str:
    return _END.format(fid=fleet_id, src=from_id)


def block(fleet_id: str, from_id: str, user: str, pubkey: str) -> str:
    """One grant, delimited so it can be found and removed again exactly."""
    return "\n".join([
        f"{begin_marker(fleet_id, from_id)} user={user}",
        pubkey.strip(),
        end_marker(fleet_id, from_id),
    ])


def posix_sync_command(fleet_id: str, from_id: str, *, user: str = "",
                       pubkey: str | None = None, path: str = "") -> str:
    """Drop our block, then optionally append a fresh one. Idempotent either way.

    Drop-then-append rather than append-if-absent: re-granting an existing edge must not
    accumulate duplicates, and a rotated key must replace rather than join the old one.
    Passing pubkey=None is the revoke.

    Matching is by line *prefix*, not equality, so a marker that picked up trailing
    whitespace somewhere still matches and the block is not orphaned.
    """
    begin, end = begin_marker(fleet_id, from_id), end_marker(fleet_id, from_id)
    # Single-quoting the default would stop $HOME expanding, leaving a literal directory
    # named "$HOME" in the working directory and a key nobody can use.
    target = shlex.quote(path) if path else '"$HOME/.ssh/authorized_keys"'
    append = ""
    if pubkey is not None:
        append = f"printf '%s\\n' {shlex.quote(block(fleet_id, from_id, user, pubkey))} >> \"$t\"\n"
    return (
        # umask before mkdir: sshd ignores a group-writable ~/.ssh, silently.
        f"umask 077; f={target}; mkdir -p \"$(dirname \"$f\")\"; "
        f"t=\"$f.fleet.$$\"; : >> \"$f\"; "
        f"awk -v b={shlex.quote(begin)} -v e={shlex.quote(end)} "
        "'index($0,e)==1{s=0;next} "
        # A block whose end marker is gone -- hand-edited, or a write interrupted before
        # the rename -- would otherwise make awk skip to EOF and take every key after it
        # with it. Any other fleet marker ends the skip, so the damage cannot spread past
        # our own block into someone else's or the user's.
        "s&&index($0,\"# fleet:\")==1{s=0} "
        "index($0,b)==1{s=1;next} !s' \"$f\" > \"$t\" && "
        + append +
        "mv \"$t\" \"$f\""
    )


def powershell_sync_command(fleet_id: str, from_id: str, *, user: str = "",
                            pubkey: str | None = None, path: str = "") -> str:
    """The Windows twin. Same contract, entirely different mechanics.

    Two traps, both of which fail *silently* -- the command succeeds and the key simply
    never works:

    * Windows OpenSSH ships a `Match Group administrators` rule pointing at
      ProgramData\\ssh\\administrators_authorized_keys. For anyone in that group -- which
      is most Windows logins -- appending to ~/.ssh/authorized_keys is ignored entirely.
    * Permissions are ACLs, and sshd refuses a key file writable by anyone but SYSTEM and
      Administrators. `chmod` and umask do nothing here.

    Because both failures report success, a grant on Windows is only ever confirmed by
    connecting on the new key -- never by this command's exit status.
    """
    begin, end = begin_marker(fleet_id, from_id), end_marker(fleet_id, from_id)
    add = ""
    if pubkey is not None:
        # One append per line, rather than one multi-line string literal split on
        # newlines. `powershell -Command -` evaluates piped input statement by
        # statement, so a literal spanning newlines is read as several broken
        # statements -- which failed silently: the script exited 0 and appended nothing.
        add = "".join(f"$keep += '{line.replace(chr(39), chr(39) * 2)}'\n"
                      for line in block(fleet_id, from_id, user, pubkey).split("\n"))
    return (
        "$ErrorActionPreference='Stop'\n"
        # An explicit path exists so the block logic -- the half that can lock someone
        # out -- can be exercised against a scratch file. The real run resolves it, and
        # resolving it wrongly is the silent failure this whole twin exists for.
        + (f"$f='{path}'\n" if path else
           "$id=[Security.Principal.WindowsIdentity]::GetCurrent()\n"
           "$admin=(New-Object Security.Principal.WindowsPrincipal($id)).IsInRole("
           "[Security.Principal.WindowsBuiltInRole]::Administrator)\n"
           "if($admin){$f=Join-Path $env:ProgramData 'ssh\\administrators_authorized_keys'}"
           "else{$f=Join-Path $env:USERPROFILE '.ssh\\authorized_keys'}\n")
        +
        "$d=Split-Path $f; if(!(Test-Path $d)){New-Item -ItemType Directory -Path $d|Out-Null}\n"
        "if(!(Test-Path $f)){New-Item -ItemType File -Path $f|Out-Null}\n"
        "$lines=@(Get-Content -LiteralPath $f -ErrorAction SilentlyContinue)\n"
        f"$b='{begin}'; $e='{end}'\n"
        "$keep=@(); $s=$false\n"
        "foreach($l in $lines){"
        "if($l.StartsWith($e)){$s=$false;continue}"
        # same bound as the POSIX twin: a missing end marker must not eat the rest
        "if($s -and $l.StartsWith('# fleet:')){$s=$false}"
        "if($l.StartsWith($b)){$s=$true;continue}"
        "if(!$s){$keep+=$l}}\n"
        + add +
        "$t=\"$f.fleet.$PID\"\n"
        "Set-Content -LiteralPath $t -Value $keep -Encoding ascii\n"
        "Move-Item -LiteralPath $t -Destination $f -Force\n"
        # inheritance:r first, or inherited ACEs survive and sshd still refuses the file
        + ("" if path else
           "icacls $f /inheritance:r /grant 'SYSTEM:F' 'Administrators:F' | Out-Null\n")
    )
