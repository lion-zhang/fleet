"""Your key, and how to get it onto a machine that does not take it yet.

fleet has no key of its own and writes no authorized_keys. It connects the way you do --
your ssh config, your agent, your keys, Tailscale SSH -- so a machine that refuses you
refuses fleet, and the fix is the one you would make anyway: put your public key on it,
once. These are the words for that, per platform, and the one standard tool that does it
for you (`ssh-copy-id`, which asks for the password itself; fleet never sees it).
"""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path

from .cmd import Endpoint

_KEY_PREFERENCE = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")


def public_key(ssh_dir: Path | None = None) -> tuple[Path, str] | None:
    """Your public key, preferring modern algorithms. None if there is none."""
    d = ssh_dir or (Path.home() / ".ssh")
    for name in _KEY_PREFERENCE:
        candidate = d / name
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return candidate, text
    for candidate in sorted(d.glob("*.pub")) if d.is_dir() else []:
        text = candidate.read_text(encoding="utf-8").strip()
        if text:
            return candidate, text
    return None


def pinned_public_key(identity: str) -> tuple[tuple[Path, str] | None, list[str]]:
    """The public half of the key a machine is pinned to (`-i`), and what to run first.

    Only that key: fleet reaches the machine with IdentitiesOnly, so another key of yours
    on it lets nothing in. A key from a cloud provider (a `.pem`) has no `.pub` beside
    it; its public half is worked out with `ssh-keygen -y`, never asking for a passphrase.
    The lines say how to write the `.pub` once, which `ssh-copy-id -i` needs.
    """
    import os
    import subprocess

    pub = Path(identity + ".pub")
    if pub.is_file() and (text := pub.read_text(encoding="utf-8").strip()):
        return (pub, text), []
    quote = (lambda x: f'"{x}"') if os.name == "nt" else shlex.quote
    make = [f"its public key is not beside it; write it once:  "
            f"ssh-keygen -y -f {quote(identity)} > {quote(identity + '.pub')}"]
    try:
        out = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", identity],
                             capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, make
    text = out.stdout.strip() if out.returncode == 0 else ""
    return ((pub, text) if text.startswith(("ssh-", "ecdsa-", "sk-")) else None), make


def posix_install_line(pubkey: str) -> str:
    """What to run on a Linux or macOS machine to let this key in."""
    return ("umask 077; mkdir -p ~/.ssh && "
            f"printf '%s\\n' {shlex.quote(pubkey.strip())} >> ~/.ssh/authorized_keys")


def windows_install_line(pubkey: str, *, admin: bool) -> str:
    """What to run in PowerShell on a Windows machine to let this key in.

    An administrator's keys live in ProgramData, not in their home, and sshd ignores that
    file unless only SYSTEM and Administrators can write it -- both fail silently, which
    is why the line does both.
    """
    quoted = pubkey.strip().replace("'", "''")
    if admin:
        return ("$f=\"$env:ProgramData\\ssh\\administrators_authorized_keys\"; "
                f"Add-Content -LiteralPath $f -Value '{quoted}'; "
                "icacls $f /inheritance:r /grant 'SYSTEM:F' 'Administrators:F'")
    return ("New-Item -ItemType Directory -Force \"$HOME\\.ssh\" | Out-Null; "
            f"Add-Content -LiteralPath \"$HOME\\.ssh\\authorized_keys\" -Value '{quoted}'")


def copy_id_argv(ep: Endpoint) -> list[str] | None:
    """`ssh-copy-id` for this endpoint, or None where there is none (Windows).

    It asks for the password itself, in the terminal, and puts your key on the machine;
    fleet only starts it and never sees what is typed.
    """
    tool = shutil.which("ssh-copy-id")
    if not tool:
        return None
    argv = [tool]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.identity:
        argv += ["-i", ep.identity]
    if ep.jump:
        argv += ["-o", f"ProxyJump={ep.jump}"]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    return argv


def remote_device_id_command(device_id: str, *, host: str = "",
                             platform: str = "posix") -> str:
    """Write the id fleet gave a clone into fleet's config folder on that machine.

    Two machines cloned from one image share a machine-id, so the id derived from it
    cannot tell them apart; the second is given `<machine-id id>:<suffix>`. Written
    where fleet on that machine looks (`identity.DEVICE_ID_FILE`) and where the probe
    reads it, so every fleet that looks at it, and every later probe, agree.

    On Linux and macOS it also goes in `device-id.HOST`, which is read first: clones that
    share a home -- containers on one volume, nodes on one NFS home -- share the folder,
    and the one id in `device-id` was read by all of them, so the next `fleet add` took
    one clone for the other. `device-id` stays as it was, for a fleet older than this.
    """
    if not re.fullmatch(r"[A-Za-z0-9:._-]{1,200}", device_id):
        raise ValueError(f"not a device id: {device_id!r}")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", host or ""):
        host = ""
    if platform == "windows":
        return ("$d=Join-Path $env:LOCALAPPDATA 'fleet';"
                "if(!(Test-Path $d)){New-Item -ItemType Directory -Path $d|Out-Null};"
                f"Set-Content -LiteralPath (Join-Path $d 'device-id') -Value '{device_id}'"
                " -Encoding ascii;'ok'")
    return ('case "$(uname -s)" in '
            'Darwin) d="$HOME/Library/Application Support/fleet" ;; '
            '*) d="$HOME/.config/fleet" ;; '
            f'esac; mkdir -p "$d" && echo {device_id} > "$d/device-id"'
            + (f' && echo {device_id} > "$d/device-id.{host}"' if host else "")
            + ' && echo ok')
