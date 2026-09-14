"""Keep the center serving, without anyone remembering to start it.

A center that only listens while someone holds a terminal open is not a center machines
can rely on, and telling the user to wire up launchd or a scheduled task themselves is
the manual step this exists to remove. So becoming the center installs the service, and
updating fleet stops it first and starts it again after -- which is not politeness: on
Windows a running fleet.exe holds its own install open, and `uv tool install` fails
against it with an error that says nothing about why.

The three platforms disagree about everything except the shape of the job, so what is
shared here is the shape -- install, remove, status, stop, start -- and each platform
supplies its own five lines.

**It always runs as the user, never as the system.** fleet keeps its access list in a
per-user directory, so a service running as SYSTEM or root would look in a different
place, find no fleet, and serve nothing -- while appearing to be up.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .sshcmd import WINDOWS, local_platform

LABEL = "io.fleet.center"
TASK = "fleet-center"
UNIT = "fleet-center.service"

ABSENT, INSTALLED, RUNNING = "absent", "installed", "running"


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


# --------------------------------------------------------------------- macOS

def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _plist(cmd: str, port: int) -> str:
    args = "".join(f"    <string>{a}</string>\n"
                   for a in (cmd, "center", "--listen", "--port", str(port)))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
"""


def _darwin_install(cmd: str, port: int) -> str:
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plist(cmd, port))
    target = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", target, str(path)])      # idempotent: ignore failure
    p = _run(["launchctl", "bootstrap", target, str(path)])
    if p.returncode != 0:
        return f"could not start it: {(p.stderr or p.stdout).strip()[:160]}"
    return f"running, and again at login ({path})"


def _darwin_remove() -> str:
    path = _plist_path()
    _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
    path.unlink(missing_ok=True)
    return "removed"


def _darwin_status() -> str:
    if not _plist_path().exists():
        return ABSENT
    p = _run(["launchctl", "list", LABEL])
    return RUNNING if p.returncode == 0 else INSTALLED


def _darwin_stop() -> None:
    _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(_plist_path())])


def _darwin_start() -> None:
    if _plist_path().exists():
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(_plist_path())])


# --------------------------------------------------------------------- Linux

def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def _unit(cmd: str, port: int) -> str:
    return f"""[Unit]
Description=fleet center
After=network-online.target

[Service]
ExecStart="{cmd}" center --listen --port {port}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _linux_install(cmd: str, port: int) -> str:
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_unit(cmd, port))
    _run(["systemctl", "--user", "daemon-reload"])
    p = _run(["systemctl", "--user", "enable", "--now", UNIT])
    if p.returncode != 0:
        return f"could not start it: {(p.stderr or p.stdout).strip()[:160]}"
    # Without this the unit stops when the last session for this user ends, which on a
    # headless box is the moment you close the ssh connection that installed it.
    _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    return f"running, and again at boot ({path})"


def _linux_remove() -> str:
    _run(["systemctl", "--user", "disable", "--now", UNIT])
    _unit_path().unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return "removed"


def _linux_status() -> str:
    if not _unit_path().exists():
        return ABSENT
    p = _run(["systemctl", "--user", "is-active", UNIT])
    return RUNNING if p.stdout.strip() == "active" else INSTALLED


def _linux_stop() -> None:
    _run(["systemctl", "--user", "stop", UNIT])


def _linux_start() -> None:
    if _unit_path().exists():
        _run(["systemctl", "--user", "start", UNIT])


# ------------------------------------------------------------------- Windows

def _windows_install(cmd: str, port: int) -> str:
    # At logon as this user, not at startup as SYSTEM: fleet's access list lives in a
    # per-user directory, so a task running as SYSTEM would look somewhere else, find no
    # fleet, and serve nothing while looking perfectly healthy.
    task = f'"{cmd}" center --listen --port {port}'
    _run(["schtasks", "/delete", "/tn", TASK, "/f"])
    p = _run(["schtasks", "/create", "/tn", TASK, "/tr", task,
              "/sc", "onlogon", "/rl", "highest", "/f"])
    if p.returncode != 0:
        return f"could not register it: {(p.stderr or p.stdout).strip()[:160]}"
    _run(["schtasks", "/run", "/tn", TASK])
    return f"running, and again at logon (scheduled task {TASK})"


def _windows_remove() -> str:
    _windows_stop()
    _run(["schtasks", "/delete", "/tn", TASK, "/f"])
    return "removed"


def _windows_status() -> str:
    p = _run(["schtasks", "/query", "/tn", TASK, "/fo", "list"])
    if p.returncode != 0:
        return ABSENT
    return RUNNING if "Running" in p.stdout else INSTALLED


def _windows_stop() -> None:
    _run(["schtasks", "/end", "/tn", TASK])
    # /end asks the task to stop; the process it started may outlive it, and on Windows a
    # running fleet.exe holds its own installation open so the next update fails.
    _run(["taskkill", "/f", "/im", "fleet.exe"])


def _windows_start() -> None:
    _run(["schtasks", "/run", "/tn", TASK])


# -------------------------------------------------------------------- dispatch

_BY_PLATFORM = {
    "darwin": (_darwin_install, _darwin_remove, _darwin_status, _darwin_stop, _darwin_start),
    "win32": (_windows_install, _windows_remove, _windows_status, _windows_stop, _windows_start),
    "linux": (_linux_install, _linux_remove, _linux_status, _linux_stop, _linux_start),
}


def _impl():
    key = "win32" if local_platform() == WINDOWS else sys.platform
    return _BY_PLATFORM.get(key, _BY_PLATFORM["linux"])


def install(cmd: str, port: int) -> str:
    return _impl()[0](cmd, port)


def remove() -> str:
    return _impl()[1]()


def status() -> str:
    return _impl()[2]()


def stop() -> None:
    """Best effort, and deliberately quiet: called before an update, where the service
    not being there is the ordinary case rather than a problem."""
    _impl()[3]()


def start() -> None:
    _impl()[4]()
