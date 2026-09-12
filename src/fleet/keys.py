"""Bootstrap key auth using a password typed once.

fleet's whole credential story is that secrets never cross into an agent transcript or
a log. A password is therefore not something to hold: it is used for exactly one
connection, to append a public key to the host's authorized_keys, and then discarded.
Every later connection is key auth, which is what the rest of the codebase already
assumes.

The password reaches ssh through a pty, never through argv -- argv is readable by every
process on the machine via `ps`.
"""

from __future__ import annotations

import os
import pty
import re
import select
import shlex
import signal
import time
from contextlib import suppress
from pathlib import Path

from .config import FLEET_KEY
from .sshcmd import Endpoint

# sshd's prompt varies ("Password:", "root@host's password:", a PAM phrasing), so match
# the one word they reliably share, case-insensitively.
_PROMPT = re.compile(rb"password.*:\s*$", re.IGNORECASE)

def ensure_keypair(path: Path | None = None) -> tuple[Path, str]:
    """This machine's own fleet keypair, created once. Returns (private path, public text).

    Dedicated rather than reusing ~/.ssh/id_*: this key is fleet's handle on the
    machine, so it can be revoked fleet-wide without touching the key you push to
    GitHub with, and an entry in someone's authorized_keys says plainly where it came
    from.

    **Never regenerates.** A new key would orphan every authorized_keys entry already
    placed for this machine -- on every host, in every fleet -- with nothing left to
    match them by and no way to find them again. Same rule, and same reason, as the age
    identity this replaces.
    """
    import socket
    import subprocess

    path = path or FLEET_KEY
    pub = path.with_suffix(".pub")
    if path.exists() and pub.exists():
        return path, pub.read_text().strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    # ssh-keygen refuses to overwrite, which is the behaviour we want, but a half-made
    # pair from an interrupted run would wedge it forever. Clear only that case.
    if path.exists() or pub.exists():
        with suppress(OSError):
            path.unlink(missing_ok=True)
        with suppress(OSError):
            pub.unlink(missing_ok=True)
    try:
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
             "-C", f"fleet:{socket.gethostname()}", "-f", str(path)],
            check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise KeyError("ssh-keygen not found -- install OpenSSH") from exc
    except subprocess.CalledProcessError as exc:
        raise KeyError(f"ssh-keygen failed: {(exc.stderr or '').strip()[:200]}") from exc
    os.chmod(path, 0o600)
    return path, pub.read_text().strip()


_KEY_PREFERENCE = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")


def public_key(ssh_dir: Path | None = None) -> tuple[Path, str] | None:
    """The public key to install, preferring modern algorithms. None if there is none."""
    d = ssh_dir or (Path.home() / ".ssh")
    for name in _KEY_PREFERENCE:
        candidate = d / name
        if candidate.is_file():
            text = candidate.read_text().strip()
            if text:
                return candidate, text
    for candidate in sorted(d.glob("*.pub")) if d.is_dir() else []:
        text = candidate.read_text().strip()
        if text:
            return candidate, text
    return None


def authorized_keys_command(pubkey: str) -> str:
    """Append the key, tightening permissions on the way.

    sshd silently ignores authorized_keys when ~/.ssh is group-writable, so umask is
    part of the operation rather than an afterthought. The key is quoted because it is
    user-supplied text being placed into a shell command.
    """
    return ("umask 077; mkdir -p ~/.ssh; "
            f"printf '%s\\n' {shlex.quote(pubkey)} >> ~/.ssh/authorized_keys")


def build_password_argv(ep: Endpoint, *, timeout: int = 15) -> list[str]:
    """ssh invocation that will actually ask for a password.

    The opposite of the probe path in sshcmd.build_argv: there BatchMode=yes guarantees
    ssh never blocks on a prompt, here the prompt is the entire point. Public key auth
    is disabled outright -- the missing key is why we are here, and leaving it enabled
    risks succeeding via some other agent-loaded key and never installing anything.
    """
    argv = [
        "ssh",
        "-o", "PubkeyAuthentication=no",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "NumberOfPasswordPrompts=1",
    ]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    # -J, but deliberately not -i: under PubkeyAuthentication=no an identity is
    # meaningless, while without the jump host a device reached through a bastion
    # cannot be bootstrapped at all.
    if ep.jump:
        argv += ["-J", ep.jump]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    return argv


def run_with_password(argv: list[str], password: str, *, timeout: float = 20.0) -> tuple[int, str]:
    """Run a command under a pty, answering the first password prompt.

    Returns (exit_code, output) with the password scrubbed from the output -- the
    caller prints that output on failure, and it must be safe to show.
    """
    pid, fd = pty.fork()
    if pid == 0:                                    # child: becomes the command
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    chunks: list[bytes] = []
    answered = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return 1, _scrub(b"".join(chunks), password) + "\n[timed out]"
            if not select.select([fd], [], [], min(0.5, remaining))[0]:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:                         # pty closes when the child exits
                break
            if not data:
                break
            chunks.append(data)
            if not answered and _PROMPT.search(b"".join(chunks[-2:]).rstrip()):
                os.write(fd, password.encode() + b"\n")
                answered = True
    finally:
        with suppress(OSError):
            os.close(fd)

    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
    return code, _scrub(b"".join(chunks), password)


def _scrub(raw: bytes, password: str) -> str:
    text = raw.decode(errors="replace")
    return text.replace(password, "***") if password else text


def install_key(ep: Endpoint, password: str, pubkey: str, *,
                timeout: float = 20.0) -> tuple[bool, str]:
    """Append pubkey to the host's authorized_keys. Returns (ok, output-safe-to-print)."""
    argv = build_password_argv(ep) + [authorized_keys_command(pubkey)]
    code, output = run_with_password(argv, password, timeout=timeout)
    return code == 0, output
