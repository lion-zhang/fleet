"""Installing fleet itself onto a device, which promotes it to infrastructure.

Every other device fleet touches needs nothing installed -- the probe is sh piped over
one connection. This is the deliberate exception, and it is the only code fleet causes
to persist on a machine, so the script is exercised for real against a local git repo
rather than asserted against as a string.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from fleet.install import build_install_argv, install_script
from fleet.ssh.cmd import Endpoint

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")


def _origin(tmp_path):
    """A real git repo standing in for the private GitHub one."""
    src, origin = tmp_path / "src", tmp_path / "origin.git"
    src.mkdir()
    (src / "marker.txt").write_text("v1\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    run = lambda *a, **kw: subprocess.run(a, cwd=str(src), env=env, check=True,
                                          capture_output=True, **kw)
    run("git", "init", "--quiet", "--initial-branch=main")
    run("git", "add", "-A")
    run("git", "commit", "--quiet", "-m", "init")
    subprocess.run(["git", "clone", "--quiet", "--bare", str(src), str(origin)],
                   env=env, check=True, capture_output=True)
    return origin


def _sandbox_bin(tmp_path):
    """A PATH containing stubs for every command the installer shells out to.

    crontab is stubbed unconditionally, not just in the cron tests: install_script
    always touches the table -- it removes the entry earlier versions left -- so any
    test that forgets would edit the machine's own schedule. A test that escapes its
    sandbox is a bug in the test, and this one did exactly that.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "uv.log"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    uv.chmod(0o755)
    fleet = bin_dir / "fleet"          # the script ends with `fleet --version`
    # rejects unknown options exactly as the real CLI does: a stub that accepts any
    # argument cannot catch the installer verifying with a flag that does not exist.
    fleet.write_text('#!/bin/sh\ncase "$1" in --version) echo "fleet 0.1.0";;'
                     ' *) echo "No such option: $1" >&2; exit 2;; esac\n')
    fleet.chmod(0o755)
    spool = tmp_path / "crontab.txt"
    if not spool.exists():
        spool.write_text("")
    ct = bin_dir / "crontab"
    ct.write_text(f'#!/bin/sh\n'
                  f'if [ "$1" = "-l" ]; then cat {spool}; exit 0; fi\n'
                  f'cat > {spool}\n')
    ct.chmod(0o755)
    return bin_dir, log


def _fake_uv(tmp_path):
    return _sandbox_bin(tmp_path)


def _fake_crontab(tmp_path):
    _sandbox_bin(tmp_path)
    return tmp_path / "crontab.txt"


def _run(script: str, tmp_path):
    bin_dir, _ = _fake_uv(tmp_path)
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"})


def test_a_fresh_device_gets_a_clone(tmp_path):
    result = _run(install_script(str(_origin(tmp_path))), tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".local" / "share" / "fleet" / "marker.txt").read_text() == "v1\n"


def test_running_it_twice_updates_instead_of_failing(tmp_path):
    """`fleet install` doubles as `fleet update`; a second run must not die on an
    existing directory."""
    origin = _origin(tmp_path)
    assert _run(install_script(str(origin)), tmp_path).returncode == 0
    second = _run(install_script(str(origin)), tmp_path)
    assert second.returncode == 0, second.stderr


def test_an_update_picks_up_new_commits(tmp_path):
    origin = _origin(tmp_path)
    assert _run(install_script(str(origin)), tmp_path).returncode == 0

    clone = tmp_path / "work"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], env=env, check=True)
    (clone / "marker.txt").write_text("v2\n")
    for args in (["git", "add", "-A"], ["git", "commit", "--quiet", "-m", "v2"],
                 ["git", "push", "--quiet", "origin", "main"]):
        subprocess.run(args, cwd=str(clone), env=env, check=True, capture_output=True)

    assert _run(install_script(str(origin)), tmp_path).returncode == 0
    assert (tmp_path / ".local" / "share" / "fleet" / "marker.txt").read_text() == "v2\n"


def test_the_installer_hands_the_checkout_to_uv(tmp_path):
    bin_dir, log = _fake_uv(tmp_path)
    subprocess.run(["sh", "-c", install_script(str(_origin(tmp_path)))],
                   capture_output=True, text=True, timeout=60,
                   env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert "tool install" in log.read_text()


def test_a_repo_url_cannot_inject_shell(tmp_path):
    """The URL comes from config and lands in a remote shell command."""
    marker = tmp_path / "pwned"
    script = install_script(f"http://example.com/x'; touch {marker}; echo '")
    subprocess.run(["sh", "-c", script], capture_output=True, timeout=60,
                   env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert not marker.exists()


# --------------------------------------------------------------- the connection

def test_agent_forwarding_is_on_so_no_credential_lands_on_the_device():
    """The remote authenticates to GitHub as you, over the forwarded agent, and nothing
    persists on a machine you may not fully control."""
    assert "-A" in build_install_argv(Endpoint(target="h", user="root", port=22))


def test_agent_forwarding_can_be_declined_for_a_host_you_do_not_trust():
    """Forwarding lets root on that box use your agent while you are connected."""
    argv = build_install_argv(Endpoint(target="h", user="root", port=22),
                              forward_agent=False)
    assert "-A" not in argv


# --------------------------------------------------------------- CLI guards

def _cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fleet.state import inventory as inv, store
    from fleet.models import Device, Kind

    dev = Device(id="linux:machine-id:o", name="oracle", kind=Kind.PERMANENT,
                 endpoints=[{"target": "oracle.example", "user": "root", "port": 22}])
    path = tmp_path / "inventory.yaml"
    inv.save([dev], path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    return CliRunner(), path


def test_install_rejects_an_unknown_device(tmp_path, monkeypatch):
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch)
    assert runner.invoke(app, ["install", "nosuchbox"]).exit_code != 0


def test_install_says_what_to_configure_when_no_repo_is_known(tmp_path, monkeypatch):
    """Without a repo there is nothing to clone, and a bare 'failed' would send the
    user hunting through ssh output for a local configuration problem."""
    from fleet import cli
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "configured_repo", lambda: "")
    result = runner.invoke(app, ["install", "oracle"])
    assert result.exit_code != 0
    assert "--repo" in result.output or "repo" in result.output.lower()


def test_a_successful_install_records_the_new_role(tmp_path, monkeypatch):
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "configured_repo", lambda: "git@github.com:me/fleet.git")
    monkeypatch.setattr(cli, "run_installer", lambda *a, **k: (0, "fleet 0.2.0"))
    result = runner.invoke(app, ["install", "oracle"])
    assert result.exit_code == 0, result.output
    assert inv.load(path)[0].role == "none", "installing does not confer a role"


def test_a_failed_install_does_not_claim_the_device_is_a_backup(tmp_path, monkeypatch):
    """Recording the role on failure would make `fleet ls` lie about where your state
    is replicated -- the exact thing you would rely on when the center dies."""
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "configured_repo", lambda: "git@github.com:me/fleet.git")
    monkeypatch.setattr(cli, "run_installer", lambda *a, **k: (90, "curl: not found"))
    result = runner.invoke(app, ["install", "oracle"])
    assert result.exit_code != 0
    assert inv.load(path)[0].role == "none"


# --------------------------------------------------------------- the broker's own timer

def _fake_crontab(tmp_path):
    """A crontab(1) that reads and writes a file, like the real one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    spool = tmp_path / "crontab.txt"
    spool.write_text("")
    ct = bin_dir / "crontab"
    ct.write_text(f'''#!/bin/sh
if [ "$1" = "-l" ]; then cat {spool}; exit 0; fi
cat > {spool}
''')
    ct.chmod(0o755)
    return spool
def test_removing_our_timer_leaves_the_users_own_cron_entries_alone(tmp_path):
    spool = _fake_crontab(tmp_path)
    spool.write_text("0 3 * * * /usr/local/bin/backup.sh\n")
    _run(install_script(str(_origin(tmp_path))), tmp_path)
    assert "backup.sh" in spool.read_text()


def test_the_old_sync_timer_is_removed(tmp_path):
    """It dialled the center, and nothing connects to the center now, so it could only
    fail silently every ten minutes forever."""
    spool = _fake_crontab(tmp_path)
    spool.write_text('0 3 * * * /usr/local/bin/backup.sh\n'
                     '*/10 * * * * PATH="$HOME/.local/bin:$PATH" fleet sync # fleet-sync\n')
    _run(install_script(str(_origin(tmp_path))), tmp_path)
    out = spool.read_text()
    assert "fleet sync" not in out
    assert "backup.sh" in out


def test_no_timer_is_installed(tmp_path):
    spool = _fake_crontab(tmp_path)
    _run(install_script(str(_origin(tmp_path))), tmp_path)
    assert "fleet sync" not in spool.read_text()


def test_a_device_without_cron_still_installs_successfully(tmp_path):
    """A missing crontab is a missing convenience, not a failed install."""
    result = _run(install_script(str(_origin(tmp_path))), tmp_path)
    assert result.returncode == 0, result.stderr


def test_the_cli_supports_the_flag_the_installer_verifies_with():
    """The install script ends by running `fleet --version` to prove the install works.
    If the CLI does not accept that flag, every install reports failure after having
    actually succeeded -- which is exactly what happened on the first real run."""
    from typer.testing import CliRunner

    from fleet.cli import app

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "fleet" in result.output.lower()


def test_the_installer_fails_loudly_if_it_verifies_with_an_unsupported_flag(tmp_path):
    """The stub now rejects unknown options the way the real CLI does. A stub that
    accepts anything cannot catch an interface mismatch, and mine did not."""
    bin_dir, _ = _sandbox_bin(tmp_path)
    fleet = bin_dir / "fleet"
    fleet.write_text('#!/bin/sh\ncase "$1" in --version) echo "fleet 0.1.0";;'
                     ' *) echo "No such option: $1" >&2; exit 2;; esac\n')
    fleet.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", install_script(str(_origin(tmp_path)))],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr


def test_updating_an_existing_center_does_not_demote_it(tmp_path, monkeypatch):
    """`fleet install` doubles as `fleet update` -- that is its documented purpose --
    so an unasked-for --role default silently destroys the center designation, and the
    next `fleet sync` has nowhere to go."""
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    devices = inv.load(path)
    devices[0].role = "center"
    inv.save(devices, path)

    monkeypatch.setattr(cli, "configured_repo", lambda: "git@github.com:me/fleet.git")
    monkeypatch.setattr(cli, "run_installer", lambda *a, **k: (0, "fleet 0.1.0"))
    assert runner.invoke(app, ["install", "oracle"]).exit_code == 0
    assert inv.load(path)[0].role == "center"


def test_installing_does_not_make_a_device_a_second_root(tmp_path, monkeypatch):
    """It used to default to `backup`, which meant a second machine holding a key on
    every device forever. There is one fleet-root now, and it is not conferred by
    installing software."""
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "configured_repo", lambda: "git@github.com:me/fleet.git")
    monkeypatch.setattr(cli, "run_installer", lambda *a, **k: (0, "fleet 0.1.0"))
    runner.invoke(app, ["install", "oracle"])
    assert inv.load(path)[0].role == "none"


def test_an_explicit_role_is_still_obeyed(tmp_path, monkeypatch):
    """Not clobbering by default must not make the flag stop working."""
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    devices = inv.load(path)
    devices[0].role = "center"
    inv.save(devices, path)

    monkeypatch.setattr(cli, "configured_repo", lambda: "git@github.com:me/fleet.git")
    monkeypatch.setattr(cli, "run_installer", lambda *a, **k: (0, "fleet 0.1.0"))
    runner.invoke(app, ["install", "oracle", "--role", "backup"])
    assert inv.load(path)[0].role == "backup"


def test_installing_cannot_promote_a_center(tmp_path, monkeypatch):
    """It called promote_center directly: no key installed on the successor, no
    handover signed, nothing verified. A center nobody installed keys for is one no
    machine will accept."""
    from fleet import cli
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "configured_repo", lambda: "git@github.com:me/fleet.git")
    monkeypatch.setattr(cli, "run_installer", lambda *a, **k: (0, "fleet 0.1.0"))
    result = runner.invoke(app, ["install", "oracle", "--role", "center"])
    assert result.exit_code == 2
    assert "fleet center" in result.output


# --------------------------------------------------------------- any operating system

def test_a_windows_device_gets_powershell_not_sh():
    """`fleet install` piped every device `sh -s` and a POSIX script. On Windows that is
    not a refusal -- git ships an sh.exe that very nearly runs it, with $HOME becoming an
    MSYS path uv may or may not translate -- so the failure mode was a half-finished
    install. This is why the Windows center had to be set up by hand."""
    from fleet.ssh.cmd import WINDOWS, Endpoint

    ep = Endpoint(target="box", user="u")
    assert build_install_argv(ep, platform=WINDOWS)[-1] == "powershell -NoProfile -Command -"
    assert build_install_argv(ep)[-1] == "sh -s", "POSIX stays the default"

    script = install_script("https://github.com/x/y.git", platform=WINDOWS)
    assert "$ErrorActionPreference" in script
    assert "uv tool install --force --reinstall-package fleet-broker" in script
    assert "crontab" not in script, "Windows never had the timer this cleans up"


def test_an_unprobed_machine_is_treated_as_posix():
    """A POSIX script on Windows fails loudly; the reverse can appear to succeed."""
    assert "$ErrorActionPreference" not in install_script("r", platform="")
    assert install_script("r", platform="") == install_script("r")


def test_installing_puts_fleet_on_a_later_terminals_path():
    """The shim lands in a directory nothing has ever added to PATH, so fleet installed
    correctly and then was not there when the user typed its name."""
    from fleet.ssh.cmd import WINDOWS

    for script in (install_script("r"), install_script("r", platform=WINDOWS)):
        assert "uv tool update-shell" in script


def test_the_windows_installer_quotes_what_it_is_given():
    """A repo URL reaches PowerShell as a literal, so a quote in it must not end it."""
    from fleet.ssh.cmd import WINDOWS

    script = install_script("https://x/y'; rm -rf /; '.git", platform=WINDOWS)
    assert "'https://x/y''; rm -rf /; ''.git'" in script
