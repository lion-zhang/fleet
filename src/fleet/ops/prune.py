"""Find the fleet keys left on machines that nothing manages any more, and take them off.

`fleet access --deny` removes a key the access list knows about. A key the list has
forgotten -- a member reinstalled with a new key, a fleet dissolved with `--force` and
started again, a machine removed while it was unreachable -- stays in `authorized_keys`
for good: nothing remembers it was put there. But every block fleet writes says what it
is (`# fleet:<fleet id>:begin from=<fingerprint> user=<account>`), so the machines
themselves can be asked, and their answer compared with what the list wants.

Two rules keep this from ever locking anyone out:

* Only this fleet's blocks are removed by default. Another fleet's are listed and left
  alone -- they may be a fleet you still run -- unless that fleet's id is named.
* A key this fleet still wants on a machine keeps at least one copy there: an old
  fleet's block holding it goes only once this fleet's own block for it is in the same
  file -- it may be the very key the center logs in with.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass

from .. import reconcile as rec
from ..ssh.authkeys import sync_command
from ..ssh.cmd import local_platform, local_shell_argv, remote_platform
from ..state import access as acl
from ..state import inventory as inv
from ..state import store

MARKER = re.compile(r"^# fleet:(?P<fid>[^:\s]+):begin from=(?P<src>\S+)(?: user=(?P<user>\S+))?")
END = re.compile(r"^# fleet:[^:\s]+:end ")
KEYLINE = re.compile(r"((?:ssh|ecdsa|sk)-[\w@.-]+\s+[A-Za-z0-9+/=]+)")

# What becomes of a block.
REMOVE, OTHER_FLEET = "remove", "other-fleet"
LAST_COPY = "kept: the only copy of a key this fleet still wants here"
BARE = "outside any fleet block: fleet cannot revoke this copy"


@dataclass
class Finding:
    machine: str
    user: str
    fleet_id: str                # "" for a key outside any fleet block
    src: str
    label: str                   # who the key belongs to, as far as this fleet knows
    action: str
    done: bool = False
    error: str = ""
    platform: str = "posix"
    device_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def list_command(platform: str) -> str:
    """Print the file fleet edits for this account, then a line saying it got to the end.

    The whole file, not only fleet's markers: a key fleet manages can also sit there
    outside any block, and that copy is the one fleet's revoke would never find. The
    same file the edit scripts resolve -- on Windows the administrators' file for an
    administrator, which is where every grant to one actually lives.
    """
    if platform == "windows":
        return ("$id=[Security.Principal.WindowsIdentity]::GetCurrent()\n"
                "$admin=(New-Object Security.Principal.WindowsPrincipal($id)).IsInRole("
                "[Security.Principal.WindowsBuiltInRole]::Administrator)\n"
                "if($admin){$f=Join-Path $env:ProgramData 'ssh\\administrators_authorized_keys'}"
                "else{$f=Join-Path $env:USERPROFILE '.ssh\\authorized_keys'}\n"
                "if(Test-Path -LiteralPath $f){Get-Content -LiteralPath $f}\n"
                "'fleet-scan-done'\n")
    return ('f="$HOME/.ssh/authorized_keys"; '
            '[ -f "$f" ] && cat "$f"; echo; echo fleet-scan-done')


def _fp(line: str) -> str:
    m = KEYLINE.search(line)
    if not m:
        return ""
    try:
        return acl.fingerprint(m.group(1))
    except acl.AccessError:
        return ""


def parse(text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """The file's fleet blocks as (fleet id, from, user), and the fingerprints of keys
    outside any block."""
    blocks, bare, inside = [], [], False
    for raw in text.splitlines():
        line = raw.strip()
        if m := MARKER.match(line):
            blocks.append((m["fid"], m["src"], m["user"] or ""))
            inside = True
        elif END.match(line) or line.startswith("# fleet:"):
            inside = False
        elif not inside and line and not line.startswith("#") and line != "fleet-scan-done":
            if fp := _fp(line):
                bare.append(fp)
    return blocks, bare


def judge(acc: acl.Access, machine: str, user: str, pins: set[str],
          blocks: list[tuple[str, str, str]], fleets: set[str],
          bare: list[str] = ()) -> list[Finding]:
    """Which of one file's blocks this fleet still wants, and what to do with the rest.

    Three rules, found the hard way on a NAS that held the center's key three times --
    bare, in an old fleet's block, and in this fleet's:

    * This fleet's block for a key is the copy to keep: it is the one `--deny` can find.
    * Another fleet's block is removed only when that fleet is named, and then only if
      it is not the last copy of a key this fleet still wants here. A wanted key whose
      own block has not been written yet (a grant still pending) is reaching the machine
      through that old copy; taking it would cut access the list says should exist.
    * A key outside any block is never touched -- fleet never edits a line it did not
      write -- but one this fleet manages is reported, since revoking it would leave
      that copy working.
    """
    wanted = {acc.center} | {s for s, d, u in acc.edges() if d in pins and u == user}
    current = {src for fid, src, _ in blocks if fid == acc.fleet_id}
    found = []
    for fid, src, _u in blocks:
        if fid == acc.fleet_id and src in wanted:
            continue
        label = acc.name_of(src) if src in acc.keys else f"a key no longer in the list ({src[:24]}...)"
        if fid != acc.fleet_id and fid not in fleets:
            action = OTHER_FLEET
        elif src in wanted and src not in current:
            action = LAST_COPY
        else:
            action = REMOVE
        found.append(Finding(machine, user, fid, src, label, action))
    for fp in dict.fromkeys(bare):
        if fp in acc.keys:
            found.append(Finding(machine, user, "", fp, acc.name_of(fp), BARE))
    return found


def _accounts(acc: acl.Access, dev, pins: set[str]) -> set[str]:
    """The accounts on a machine whose files this fleet writes: the one it is reached as,
    and every account a grant names there."""
    eps = inv.endpoints_of(dev)
    users = {e.user for e in eps[:1] if e.user}
    users |= {u for _s, d, u in acc.edges() if d in pins}
    return users or {"root"}


def scan(acc: acl.Access, *, fleets: set[str] = frozenset(), only: str = "") -> tuple[
        list[Finding], list[str]]:
    """Ask every machine the center can reach. Returns findings and machines not read."""
    from .sweep import endpoint_for

    devices = [d for d in inv.live(inv.load()) if not only or d.name == only]
    findings: list[Finding] = []
    unread: list[str] = []
    conn = store.connect()
    try:
        for dev in devices:
            pins = {fp for fp, m in acc.keys.items() if m.get("device_id") == dev.id}
            here = acc.center in pins
            if not here and not inv.endpoints_of(dev):
                continue
            _, snap = store.latest(conn, dev.id)
            platform = local_platform() if here else remote_platform(snap)
            # This machine's own file is read as whoever runs fleet here, once.
            users = sorted(_accounts(acc, dev, pins))
            for user in users[:1] if here else users:
                if here:
                    p = subprocess.run(local_shell_argv(), input=list_command(platform).encode(),
                                       capture_output=True)
                    ok, out = p.returncode == 0, (p.stdout or b"").decode(errors="replace")
                else:
                    ep = endpoint_for(dev, user)
                    ok, out = rec._remote(ep, list_command(platform), platform=platform,
                                          capture=True)
                if not ok or "fleet-scan-done" not in (out or ""):
                    unread.append(f"{dev.name} ({user})")
                    continue
                blocks, bare = parse(out)
                batch = judge(acc, dev.name, user, pins, blocks, fleets, bare)
                for f in batch:
                    f.platform, f.device_id = platform, dev.id
                findings += batch
    finally:
        conn.close()
    return findings, unread


def remove(acc: acl.Access, findings: list[Finding]) -> None:
    """Take off every block judged REMOVE, one edit each, recording how it went."""
    from .sweep import endpoint_for

    devices = {d.id: d for d in inv.live(inv.load())}
    for f in findings:
        if f.action != REMOVE:
            continue
        platform = f.platform
        dev = devices.get(f.device_id)
        script = sync_command(f.fleet_id, f.src, user=f.user, pubkey=None, platform=platform)
        pins = {fp for fp, m in acc.keys.items() if dev and m.get("device_id") == dev.id}
        if acc.center in pins:
            p = subprocess.run(local_shell_argv(), input=script.encode(), capture_output=True)
            ok, out = p.returncode == 0, (p.stdout + p.stderr).decode(errors="replace")
        elif dev is not None:
            ok, out = rec._remote(endpoint_for(dev, f.user), script, platform=platform)
        else:
            ok, out = False, "the machine is no longer in the inventory"
        f.done, f.error = ok, "" if ok else (out or "").strip()[-160:]
