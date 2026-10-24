"""Run one short script on a machine, over ssh, and say how it went.

For the few things fleet changes on a machine it looks at: the id a clone is given, and
the notes `fleet edit --path` writes (machinefile.py). Never multiplexed: ControlMaster
paths are hashed per host, so a probe fan-out and this would share one master -- and
`runner._kill` kills the whole process group on a probe timeout, which could tear the
connection down mid-write.
"""

from __future__ import annotations

import subprocess

from ..ssh.cmd import WINDOWS, Endpoint, build_argv, run as sshrun


def run_script(ep: Endpoint, script: str, *, platform: str = "posix", timeout: int = 30,
               capture: bool = False) -> tuple[bool, str]:
    """(worked, what it said). With `capture`, stdout; otherwise the tail of stderr."""
    remote = "powershell -NoProfile -Command -" if platform == WINDOWS else "sh -s"
    argv = build_argv(ep, remote=remote, multiplex=False, connect_timeout=10)
    try:
        # Bytes: text mode rewrites \n to \r\n on Windows, and a CRLF-mangled script
        # half-executes.
        p = sshrun(argv, input=script.encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, str(exc)
    if capture:
        return p.returncode == 0, (p.stdout or p.stderr or b"").decode(errors="replace")
    return p.returncode == 0, (p.stderr or p.stdout or b"").decode(errors="replace").strip()[-300:]
