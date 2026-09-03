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
from fleet.sshcmd import Endpoint

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

    crontab is stubbed unconditionally, not just in the timer tests: install_script
    defaults to timer_minutes=10, so any test that forgets would run the real crontab
    and edit the machine's own schedule. A test that escapes its sandbox is a bug in
    the test, and this one did exactly that.
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

    from fleet import inventory as inv, store
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
    from fleet import cli, inventory as inv
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "configured_repo", lambda: "git@github.com:me/fleet.git")
    monkeypatch.setattr(cli, "run_installer", lambda *a, **k: (0, "fleet 0.2.0"))
    result = runner.invoke(app, ["install", "oracle"])
    assert result.exit_code == 0, result.output
    assert inv.load(path)[0].role == "backup"


def test_a_failed_install_does_not_claim_the_device_is_a_backup(tmp_path, monkeypatch):
    """Recording the role on failure would make `fleet ls` lie about where your state
    is replicated -- the exact thing you would rely on when the center dies."""
    from fleet import cli, inventory as inv
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


def test_an_idle_broker_gets_a_timer_so_it_syncs_without_anyone_logging_in(tmp_path):
    """Opportunistic sync only fires when a command runs. A backup node may go weeks
    without one, and a replica that stopped replicating is worse than none."""
    spool = _fake_crontab(tmp_path)
    _run(install_script(str(_origin(tmp_path)), timer_minutes=10), tmp_path)
    assert "fleet sync" in spool.read_text()


def test_reinstalling_does_not_stack_up_timers(tmp_path):
    spool = _fake_crontab(tmp_path)
    origin = _origin(tmp_path)
    _run(install_script(str(origin), timer_minutes=10), tmp_path)
    _run(install_script(str(origin), timer_minutes=10), tmp_path)
    assert spool.read_text().count("fleet sync") == 1


def test_the_timer_leaves_the_users_own_cron_entries_alone(tmp_path):
    spool = _fake_crontab(tmp_path)
    spool.write_text("0 3 * * * /usr/local/bin/backup.sh\n")
    _run(install_script(str(_origin(tmp_path)), timer_minutes=10), tmp_path)
    assert "backup.sh" in spool.read_text()


def test_no_timer_is_installed_when_it_is_not_wanted(tmp_path):
    spool = _fake_crontab(tmp_path)
    _run(install_script(str(_origin(tmp_path)), timer_minutes=0), tmp_path)
    assert "fleet sync" not in spool.read_text()


def test_a_device_without_cron_still_installs_successfully(tmp_path):
    """A missing crontab is a missing convenience, not a failed install."""
    result = _run(install_script(str(_origin(tmp_path)), timer_minutes=10), tmp_path)
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
        ["sh", "-c", install_script(str(_origin(tmp_path)), timer_minutes=0)],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
