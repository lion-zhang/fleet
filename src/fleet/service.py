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
import time
from pathlib import Path

from .ssh.cmd import WINDOWS, local_platform

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

def _windowless() -> str:
    """The interpreter that runs without a console, or "" if there is not one.

    Windows gives a console to anything built against the console subsystem, and the
    `fleet` launcher uv writes is one -- so the center served the fleet with a terminal
    window sitting open on the desktop for as long as it ran. `pythonw.exe` is the same
    interpreter built against the GUI subsystem and lives beside the one already running,
    so `pythonw -m fleet` gets no window at all: not a hidden one, and not one that
    flashes on the way past.
    """
    exe = Path(sys.executable)
    for name in ("pythonw.exe", "pythonw3.exe"):
        candidate = exe.with_name(name)
        if candidate.exists():
            return str(candidate)
    return ""


def _windows_install(cmd: str, port: int) -> str:
    # At logon as this user, not at startup as SYSTEM: fleet's access list lives in a
    # per-user directory, so a task running as SYSTEM would look somewhere else, find no
    # fleet, and serve nothing while looking perfectly healthy.
    quiet = _windowless()
    task = (f'"{quiet}" -m fleet center --listen --port {port}' if quiet
            else f'"{cmd}" center --listen --port {port}')
    _run(["schtasks", "/delete", "/tn", TASK, "/f"])
    p = _run(["schtasks", "/create", "/tn", TASK, "/tr", task,
              "/sc", "onlogon", "/rl", "highest", "/f"])
    if p.returncode != 0:
        return f"could not register it: {(p.stderr or p.stdout).strip()[:160]}"
    _run(["schtasks", "/run", "/tn", TASK])
    opened = _windows_open_port(port)
    return (f"running, and again at logon (scheduled task {TASK})"
            + (f"; {opened}" if opened else ""))


FIREWALL_RULE = "fleet center"


def _windows_open_port(port: int) -> str:
    """Let the fleet actually reach it.

    Windows blocks inbound by default, so the port needs a rule. Two things make that
    less obvious than it sounds, and both were found the hard way -- the listener bound
    0.0.0.0, answered on localhost and its own tailnet address, and was invisible from
    every other machine, which is the worst way for this to fail.

    **The program that listens is not fleet.exe.** That is a launcher shim; the socket
    belongs to the Python interpreter behind it, and a program rule naming fleet.exe
    matches nothing that ever accepts a connection.

    **A block rule beats an allow rule**, whatever the allow rule says. Windows quietly
    writes one for any program that tries to listen and is refused at the prompt, so an
    interpreter that was once denied stays denied and the port rule is simply overridden.
    Those are removed here, scoped to fleet's own uv-managed interpreter -- not to Python
    generally, which the user may have blocked deliberately.
    """
    # Whatever ends up holding the socket: the windowless interpreter when the task
    # uses one, and the ordinary one otherwise. Naming the wrong one is not a visible
    # failure -- the port rule still lets connections in, and Windows simply offers to
    # block the interpreter again the next time it listens.
    exe = _windowless() or sys.executable
    removed = _windows_unblock(exe)
    _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])
    p = _run(["netsh", "advfirewall", "firewall", "add", "rule",
              f"name={FIREWALL_RULE}", "dir=in", "action=allow",
              "protocol=TCP", f"localport={port}"])
    # The program rule as well as the port rule: the port rule is what makes it reachable,
    # and this is what stops Windows offering to block the interpreter again later.
    _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE} app"])
    _run(["netsh", "advfirewall", "firewall", "add", "rule",
          f"name={FIREWALL_RULE} app", "dir=in", "action=allow", f"program={exe}",
          "enable=yes"])
    if p.returncode != 0:
        return f"could not open TCP {port}: machines will not reach it"
    return f"opened TCP {port} inbound" + (f", and cleared {removed} block rule(s) on its "
                                           "interpreter" if removed else "")


def _windows_unblock(exe: str) -> int:
    """Drop inbound block rules on fleet's own interpreter. Returns how many.

    Matched on the uv python directory rather than the exact path, because the blocked
    rule and the running interpreter differ by patch version -- uv writes
    cpython-3.12.14-... into the rule and runs from cpython-3.12-... -- so an exact
    comparison finds nothing while the block still applies.
    """
    script = (
        "Get-NetFirewallRule -Direction Inbound -Action Block | "
        "Where-Object { ($_ | Get-NetFirewallApplicationFilter "
        "-ErrorAction SilentlyContinue).Program -like '*uv\\python\\*python.exe' } | "
        "ForEach-Object { Remove-NetFirewallRule -Name $_.Name; 'removed' }"
    )
    p = _run(["powershell", "-NoProfile", "-Command", script])
    return p.stdout.count("removed") if p.returncode == 0 else 0


def _windows_remove() -> str:
    _windows_stop()
    _run(["schtasks", "/delete", "/tn", TASK, "/f"])
    for name in (FIREWALL_RULE, f"{FIREWALL_RULE} app"):
        _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
    return "removed, and the firewall rules with it"


def _windows_status() -> str:
    p = _run(["schtasks", "/query", "/tn", TASK, "/fo", "list"])
    if p.returncode != 0:
        return ABSENT
    return RUNNING if "Running" in p.stdout else INSTALLED


def _windows_stop() -> None:
    _run(["schtasks", "/end", "/tn", TASK])
    # /end asks the task to stop; the process it started may outlive it, and on Windows a
    # running fleet holds its own installation open, so the next update fails against it
    # with an error that mentions nothing about why.
    #
    # By image name for the launcher, because `fleet.exe` is ours and nothing else is
    # called that. Never by image name for the interpreter: `pythonw.exe` is whatever the
    # user happens to be running, and `taskkill /im pythonw.exe` would end all of it. So
    # the windowless center is matched on its command line and killed by pid.
    _run(["taskkill", "/f", "/im", "fleet.exe"])
    _run(["powershell", "-NoProfile", "-Command",
          "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
          "Where-Object { $_.CommandLine -like '*-m*fleet*center*--listen*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"])
    _windows_await_exit()


def _windows_await_exit(timeout_s: float = 10.0) -> bool:
    """Wait until nothing fleet started is still running. Returns whether it settled.

    Asking Windows to end a process returns before the process has let go of its files,
    and `uv tool install` then fails half way through with "failed to remove directory
    ...\\Scripts: Access is denied" -- which leaves no working `fleet` on the machine at
    all, because by then it has deleted most of it. That is the whole reason updating
    stops the service first, so returning early defeats the point.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        p = _run(["powershell", "-NoProfile", "-Command",
                  "@(Get-Process fleet -ErrorAction SilentlyContinue) + "
                  "@(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*-m*fleet*center*--listen*' }) "
                  "| Measure-Object | ForEach-Object { $_.Count }"])
        if (p.stdout or "").strip() in ("0", ""):
            return True
        time.sleep(0.5)
    return False


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


def status(port: int = 0) -> str:
    """Whether it is installed, and whether it is actually serving.

    The platform's own answer is not enough: a scheduled task reported `Running` while
    the process it started sat behind a closed firewall port, reachable from nowhere.
    So a claim of running is checked against the port before it is repeated.
    """
    state = _impl()[2]()
    if state == RUNNING and port and not _answers(port):
        return INSTALLED
    return state


def _answers(port: int, timeout: float = 1.5) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def stop() -> None:
    """Best effort, and deliberately quiet: called before an update, where the service
    not being there is the ordinary case rather than a problem."""
    _impl()[3]()


def start() -> None:
    _impl()[4]()
