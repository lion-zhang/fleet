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

from .sshcmd import Endpoint

# sshd's prompt varies ("Password:", "root@host's password:", a PAM phrasing), so match
# the one word they reliably share, case-insensitively.
_PROMPT = re.compile(rb"password.*:\s*$", re.IGNORECASE)

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


def askpass_script(directory: Path) -> Path:
    """A helper ssh can call for the password, so the user keeps a real terminal.

    SSH_ASKPASS is the only way to answer ssh's prompt without sitting between the user
    and their shell. The password is read from the environment rather than written into
    this file: a file would outlive the connection and defeat the point of encrypting
    the secret at rest.
    """
    path = directory / "fleet-askpass"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as fh:
        fh.write('#!/bin/sh\nprintf \'%s\\n\' "$FLEET_ASKPASS"\n')
    return path


def install_key(ep: Endpoint, password: str, pubkey: str, *,
                timeout: float = 20.0) -> tuple[bool, str]:
    """Append pubkey to the host's authorized_keys. Returns (ok, output-safe-to-print)."""
    argv = build_password_argv(ep) + [authorized_keys_command(pubkey)]
    code, output = run_with_password(argv, password, timeout=timeout)
    return code == 0, output
