"""Keeping the center serving, on whichever OS it happens to be.

A center that only listens while someone holds a terminal open is not one machines can
rely on, and wiring up launchd or a scheduled task by hand is the step this removes. All
three platforms are generated and dispatched here, because the center is as likely to be
a Mac or a Linux box as the Windows mini PC it happens to be today.
"""

from __future__ import annotations

import sys

import pytest

from fleet import service


def _impl_for(monkeypatch, platform, sshcmd_platform="posix"):
    monkeypatch.setattr(service, "local_platform", lambda: sshcmd_platform)
    monkeypatch.setattr(service.sys, "platform", platform)
    return service._impl()


# ------------------------------------------------------------------ dispatch

@pytest.mark.parametrize("platform,sshcmd,expected", [
    ("darwin", "posix", "_darwin_install"),
    ("linux", "posix", "_linux_install"),
    ("win32", "windows", "_windows_install"),
])
def test_each_platform_gets_its_own_implementation(monkeypatch, platform, sshcmd, expected):
    assert _impl_for(monkeypatch, platform, sshcmd)[0].__name__ == expected


def test_an_unknown_platform_falls_back_to_systemd(monkeypatch):
    """A BSD or something newer is far likelier to speak systemd than launchd, and a
    wrong guess here fails loudly at install rather than silently at serve."""
    assert _impl_for(monkeypatch, "freebsd14")[0].__name__ == "_linux_install"


# ------------------------------------------------------------------ what gets written

def test_the_launchd_job_restarts_and_runs_at_login():
    plist = service._plist("/usr/local/bin/fleet", 7373)
    assert "<key>RunAtLoad</key><true/>" in plist
    assert "<key>KeepAlive</key><true/>" in plist, "a center that dies stays dead otherwise"
    assert "<string>/usr/local/bin/fleet</string>" in plist
    assert "<string>--port</string>" in plist and "<string>7373</string>" in plist


def test_the_systemd_unit_restarts_and_survives_logout():
    unit = service._unit("/home/lin/.local/bin/fleet", 7373)
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    # a path with a space would otherwise split into two arguments
    assert 'ExecStart="/home/lin/.local/bin/fleet" center --listen --port 7373' in unit


def test_a_path_with_spaces_survives_every_platform():
    """`fleet_executable()` can answer with an absolute path under a directory nobody
    chose -- "Application Support" is two words on the platform most likely to be a
    laptop center."""
    odd = "/Users/lin/Library/Application Support/fleet/bin/fleet"
    assert f"<string>{odd}</string>" in service._plist(odd, 1)
    assert f'ExecStart="{odd}"' in service._unit(odd, 1)


# ------------------------------------------------------------------ the commands run

def test_windows_registers_for_this_user_not_the_system(monkeypatch):
    """fleet keeps its access list in a per-user directory, so a task running as SYSTEM
    would look somewhere else, find no fleet, and serve nothing while looking healthy."""
    calls = []
    monkeypatch.setattr(service, "_run",
                        lambda argv, **k: calls.append(argv) or _ok())
    service._windows_install("C:\\fleet.exe", 7373)
    create = next(c for c in calls if "/create" in c)
    assert "/sc" in create and create[create.index("/sc") + 1] == "onlogon"
    assert "/ru" not in create, "no /ru means the invoking user, which is the point"
    assert "SYSTEM" not in " ".join(create)


def test_stopping_on_windows_also_kills_the_process(monkeypatch):
    """`schtasks /end` asks the task to stop; the process it started can outlive it, and
    a live fleet.exe holds its own installation open so the next update fails."""
    calls = []
    monkeypatch.setattr(service, "_run", lambda argv, **k: calls.append(argv) or _ok())
    service._windows_stop()
    flat = [" ".join(c) for c in calls]
    assert any("/end" in f for f in flat)
    assert any("taskkill" in f for f in flat)


def test_starting_does_nothing_when_nothing_is_installed(monkeypatch, tmp_path):
    """`fleet install` is idempotent and runs on every machine, so a start must not
    conjure a service onto one that never had one."""
    monkeypatch.setattr(service, "_unit_path", lambda: tmp_path / "nope.service")
    calls = []
    monkeypatch.setattr(service, "_run", lambda argv, **k: calls.append(argv) or _ok())
    service._linux_start()
    assert not calls

    monkeypatch.setattr(service, "_plist_path", lambda: tmp_path / "nope.plist")
    service._darwin_start()
    assert not calls


def test_status_reads_absent_before_installed_before_running(monkeypatch, tmp_path):
    unit = tmp_path / UNITNAME
    monkeypatch.setattr(service, "_unit_path", lambda: unit)
    assert service._linux_status() == service.ABSENT
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(service, "_run", lambda *a, **k: _ok(stdout="inactive\n"))
    assert service._linux_status() == service.INSTALLED
    monkeypatch.setattr(service, "_run", lambda *a, **k: _ok(stdout="active\n"))
    assert service._linux_status() == service.RUNNING


UNITNAME = "fleet-center.service"


def _ok(stdout="", returncode=0):
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout, "")


# ------------------------------------------------------------------ the installer

def test_the_installer_stops_the_service_before_replacing_it():
    """On Windows a live fleet.exe holds its own installation open, and `uv tool install`
    fails against it with an error that mentions nothing about why."""
    from fleet.install import install_script

    sc = install_script("git@example.com:x/y.git")
    assert sc.index("service stop") < sc.index("uv tool install") < sc.index("service start")
    # --force alone reuses a cached wheel and ships stale code; --reinstall rebuilds
    # every dependency, which turns a deploy into a download of the world
    assert "--reinstall-package fleet-broker" in sc
    assert "--reinstall " not in sc


def test_windows_allows_the_interpreter_not_the_shim(monkeypatch):
    """fleet.exe is a launcher; the socket belongs to the Python behind it. A program
    rule naming fleet.exe matches nothing that ever accepts a connection."""
    calls = []
    monkeypatch.setattr(service, "_run", lambda argv, **k: calls.append(argv) or _ok())
    monkeypatch.setattr(service.sys, "executable", "C:\\uv\\python\\python.exe")
    service._windows_open_port(7373)
    flat = [" ".join(c) for c in calls]
    assert any("localport=7373" in f for f in flat), "the port itself must be opened"
    assert any("program=C:\\uv\\python\\python.exe" in f for f in flat)
    assert not any("program=" in f and "fleet.exe" in f for f in flat)


def test_windows_clears_a_block_rule_on_its_own_interpreter(monkeypatch):
    """A block rule beats an allow rule whatever the allow rule says, and Windows writes
    one for any program refused at the prompt -- so the port rule is simply overridden."""
    calls = []
    monkeypatch.setattr(service, "_run", lambda argv, **k: calls.append(argv) or _ok())
    service._windows_unblock("C:\\uv\\python\\python.exe")
    script = " ".join(calls[0])
    assert "Action Block" in script and "Remove-NetFirewallRule" in script
    # scoped to fleet's own interpreter, not to Python generally: the user may have
    # blocked another one deliberately
    assert "uv" in script and "python.exe" in script
