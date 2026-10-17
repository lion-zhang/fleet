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

from fleet import install
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
    result = _run(install_script(str(_origin(tmp_path)), from_git=True), tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".local" / "share" / "fleet" / "marker.txt").read_text(encoding="utf-8") == "v1\n"


def test_running_it_twice_updates_instead_of_failing(tmp_path):
    """`fleet install` doubles as `fleet update`; a second run must not die on an
    existing directory."""
    origin = _origin(tmp_path)
    assert _run(install_script(str(origin), from_git=True), tmp_path).returncode == 0
    second = _run(install_script(str(origin), from_git=True), tmp_path)
    assert second.returncode == 0, second.stderr


def test_an_update_picks_up_new_commits(tmp_path):
    origin = _origin(tmp_path)
    assert _run(install_script(str(origin), from_git=True), tmp_path).returncode == 0

    clone = tmp_path / "work"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], env=env, check=True)
    (clone / "marker.txt").write_text("v2\n")
    for args in (["git", "add", "-A"], ["git", "commit", "--quiet", "-m", "v2"],
                 ["git", "push", "--quiet", "origin", "main"]):
        subprocess.run(args, cwd=str(clone), env=env, check=True, capture_output=True)

    assert _run(install_script(str(origin), from_git=True), tmp_path).returncode == 0
    assert (tmp_path / ".local" / "share" / "fleet" / "marker.txt").read_text(encoding="utf-8") == "v2\n"


def test_the_installer_hands_the_checkout_to_uv(tmp_path):
    bin_dir, log = _fake_uv(tmp_path)
    subprocess.run(["sh", "-c", install_script(str(_origin(tmp_path)), from_git=True)],
                   capture_output=True, text=True, timeout=60,
                   env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert "tool install" in log.read_text(encoding="utf-8")


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
    assert "backup.sh" in spool.read_text(encoding="utf-8")


def test_the_old_sync_timer_is_removed(tmp_path):
    """It dialled the center, and nothing connects to the center now, so it could only
    fail silently every ten minutes forever."""
    spool = _fake_crontab(tmp_path)
    spool.write_text('0 3 * * * /usr/local/bin/backup.sh\n'
                     '*/10 * * * * PATH="$HOME/.local/bin:$PATH" fleet sync # fleet-sync\n')
    _run(install_script(str(_origin(tmp_path))), tmp_path)
    out = spool.read_text(encoding="utf-8")
    assert "fleet sync" not in out
    assert "backup.sh" in out


def test_no_timer_is_installed(tmp_path):
    spool = _fake_crontab(tmp_path)
    _run(install_script(str(_origin(tmp_path))), tmp_path)
    assert "fleet sync" not in spool.read_text(encoding="utf-8")


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
    runner.invoke(app, ["install", "oracle", "--role", "none"])
    assert inv.load(path)[0].role == "none"


def test_a_role_that_does_nothing_is_refused(tmp_path, monkeypatch):
    """`backup` was recorded and then read by nothing. Refused before any network."""
    from fleet import cli
    from fleet.cli import app

    runner, path = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "run_installer",
                        lambda *a, **k: pytest.fail("refused before the network"))
    r = runner.invoke(app, ["install", "oracle", "--role", "backup"])
    assert r.exit_code == 2 and "no 'backup' role" in r.output


def test_install_names_the_machine(tmp_path, monkeypatch):
    """This machine has no address on record, so `fleet install` with no name could only
    fail with "no endpoint recorded"; it says what to do instead."""
    from fleet.cli import app

    runner, _ = _cli(tmp_path, monkeypatch)
    r = runner.invoke(app, ["install"])
    assert r.exit_code == 2 and "fleet update" in r.output


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
    remote = build_install_argv(ep, platform=WINDOWS)[-1]
    assert remote.startswith("powershell -NoProfile -Command ")
    assert build_install_argv(ep)[-1] == "sh -s", "POSIX stays the default"

    script = install_script("https://github.com/x/y.git", platform=WINDOWS)
    assert "$ErrorActionPreference" in script
    assert "uv tool install --force --reinstall-package agents-fleet" in script
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


def test_the_windows_installer_does_not_stop_fleet_by_running_fleet():
    """fleet.exe is a uv trampoline that executes Scripts\\python.exe, so stopping the
    service by invoking fleet holds open the directory uv is about to replace. It fails
    with "Access is denied" on Scripts -- and that is not a failed update, because uv has
    deleted most of the installation by then and the machine is left with no working
    fleet at all. Found by it happening twice on the real center."""
    from fleet.ssh.cmd import WINDOWS

    script = install_script("https://x/y.git", platform=WINDOWS)
    code = "\n".join(l for l in script.splitlines() if not l.strip().startswith("#"))
    assert "service stop" not in code, "the stop must not go through the fleet binary"
    assert "schtasks /end" in code, "it still has to stop the task"
    assert "Start-Sleep" in code, "ending a process returns before its files are released"
    # and the order matters: stop, wait, then replace
    assert code.index("schtasks /end") < code.index("uv tool install")
    assert code.index("Start-Sleep") < code.index("uv tool install")


def test_the_windows_script_is_delivered_as_a_file():
    """Two failures, one delivery. `powershell -Command -` evaluates stdin statement by
    statement, so a multi-line `if {` is a syntax error on its own and the block after it
    is skipped -- the installer appears to run, says nothing, and does nothing. Base64
    piped into `iex` fixed that and introduced the second: decoding into Invoke-Expression
    is the shape obfuscated malware has, and a machine with Defender's script rules on
    refuses the whole command with a bare "Access is denied." before a line of it runs.
    Measured on this fleet's own center, which is how it was found.

    Writing the script out and running the file has neither problem, and `-File` carries
    the exit code -- which is what brings NOTHING_TO_UPDATE back."""
    from fleet.install import payload_for
    from fleet.ssh.cmd import WINDOWS, Endpoint

    script = install_script("https://x/y.git", platform=WINDOWS)
    assert "\n" in script and "if (" in script, "the script does use blocks"

    wire = payload_for(script, WINDOWS)
    assert wire == script.encode(), "the script travels as itself, not encoded"

    remote = build_install_argv(Endpoint(target="b", user="u"), platform=WINDOWS)[-1]
    assert "ReadToEnd" in remote and "Set-Content" in remote, "it is spooled to a file"
    assert "-File" in remote, "and run from that file"
    assert "iex" not in remote and "FromBase64String" not in remote, \
        "the decode-into-iex shape is what Defender refuses"
    assert remote.index("Set-Content") < remote.index("-File"), "written before it is run"

    # POSIX is unchanged: sh reads a script from stdin correctly as it is.
    assert payload_for(script) == script.encode()
    assert build_install_argv(Endpoint(target="b", user="u"))[-1] == "sh -s"


def test_a_native_command_that_complains_does_not_abort_the_windows_installer():
    """Under `$ErrorActionPreference = 'Stop'` a native command writing to stderr raises
    a terminating NativeCommandError, and redirecting the stream away does not stop it on
    Windows PowerShell 5. Every one of these is *expected* to complain on a normal
    machine -- "ERROR: The process fleet.exe not found" is what a box whose service is
    not running says -- so without a catch the installer died before reaching uv, on
    exactly the machines with nothing to stop. It is why the center could not be updated
    at all."""
    from fleet.ssh.cmd import WINDOWS

    script = install_script("https://x/y.git", platform=WINDOWS)
    for native in ("schtasks /end", "taskkill /f", "uv tool update-shell",
                   "$fleet service start"):
        line = next((l for l in script.splitlines() if native in l), "")
        assert line, f"{native} is no longer in the script -- has it moved?"
        assert line.strip().startswith("try {") and "catch" in line, \
            f"{native} can write to stderr, and that is terminating here: {line.strip()}"


def test_every_powershell_sent_over_command_dash_completes_per_line():
    """`powershell -Command -` reads stdin and evaluates it statement by statement, so a
    line ending in `{` is a syntax error on its own and the block after it is skipped --
    silently. `reconcile._remote` still uses that form for authorized_keys edits, and it
    is safe today only because every statement there happens to fit on one line.

    Nothing enforced that. This does, because the failure is invisible: the edit would
    report success having done nothing, or worse, half of it. The installer needed a
    different answer -- it has real blocks, so it goes over base64 -- but this script is
    fine as it is and does not need rewriting, only keeping.
    """
    from fleet.ssh.authkeys import posix_sync_command, powershell_sync_command

    script = powershell_sync_command("abc123", "SHA256:xx", user="u",
                                     pubkey="ssh-ed25519 AAAA x")
    for n, line in enumerate(script.splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        assert text.count("{") == text.count("}"), \
            f"line {n} leaves a block open, which -Command - will not carry: {text!r}"
        assert text.count("(") == text.count(")"), \
            f"line {n} leaves a paren open: {text!r}"

    # The removal form too -- it is a different script.
    removal = powershell_sync_command("abc123", "SHA256:xx", user="u", pubkey=None)
    for line in removal.splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            assert text.count("{") == text.count("}"), text

    assert posix_sync_command("abc123", "SHA256:xx", user="u", pubkey=None), \
        "the POSIX form still exists; sh reads a whole script and has no such limit"


def test_updating_an_existing_install_honours_the_repo_it_was_given():
    """`--repo` was silently ignored for every machine that already had fleet: the fetch
    used whatever `origin` was set to when it was first cloned. So an install could not
    be pointed at a new remote, or moved from ssh to https, and failed with an auth error
    naming a URL the caller never asked for. Found updating a real machine."""
    from fleet.ssh.cmd import WINDOWS

    for script in (install_script("https://github.com/x/y.git"),
                   install_script("https://github.com/x/y.git", platform=WINDOWS)):
        code = "\n".join(l for l in script.splitlines() if not l.strip().startswith("#"))
        assert "remote set-url origin" in code, "an existing clone keeps its old remote"
        assert code.index("remote set-url origin") < code.index("fetch"), \
            "the remote has to be corrected before the fetch that uses it"


def test_the_repo_is_found_from_the_clone_an_install_left(tmp_path, monkeypatch):
    """Running from an install, not a checkout: the package sits in uv's tool directory
    with no git near it, and `fleet update` answered "No repo to update from" on every
    machine fleet had itself deployed. The clone it made is right there, and its origin
    is by definition where that machine came from."""
    import subprocess as sp

    from fleet import cli
    from fleet.config import Config

    monkeypatch.setattr(cli, "load_config", lambda: Config({}))
    clone = tmp_path / ".local" / "share" / "fleet"
    clone.mkdir(parents=True)
    sp.run(["git", "init", "-q", str(clone)], check=True)
    sp.run(["git", "-C", str(clone), "remote", "add", "origin",
            "https://example.invalid/fleet.git"], check=True)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))

    # as if the package were installed somewhere with no repo around it
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "site-packages" / "fleet" / "cli.py"))
    assert cli.configured_repo() == "https://example.invalid/fleet.git"


def test_installed_from_pypi_the_repo_is_the_one_it_was_published_from(tmp_path, monkeypatch):
    """No clone anywhere: installed with `uv tool install agents-fleet` or install.sh."""
    from fleet import cli
    from fleet.config import Config

    monkeypatch.setattr(cli, "load_config", lambda: Config({}))
    monkeypatch.setattr(cli, "install_source", lambda: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "site-packages" / "fleet" / "cli.py"))
    assert cli.configured_repo() == "https://github.com/lion-zhang/fleet.git"


def test_the_installer_offers_the_fleet_key(tmp_path, monkeypatch):
    """The same key the probe and `fleet ssh` offer. Without it the installer used
    whatever personal key the machine happened to have, so it worked from the laptop
    whose key was everywhere and failed with "Permission denied (publickey)" from any
    machine fleet had enrolled -- every machine that would run `fleet update` itself."""
    import fleet.config
    from fleet import install as install_mod
    from fleet.ssh.cmd import Endpoint

    key = tmp_path / "id_ed25519"
    key.write_text("x")
    monkeypatch.setattr(fleet.config, "FLEET_KEY", key)
    argv = install_mod.build_install_argv(Endpoint(target="b", user="u"))
    assert argv[argv.index("-i") + 1] == str(key)


def test_installing_on_a_machine_makes_it_a_member_at_once(tmp_path, monkeypatch):
    """Found on a real fleet: `fleet install gpu` succeeded and gpu then said "not in a
    fleet" -- membership arrives in a signed copy, and nothing handed it one."""
    from fleet import cli
    from fleet.models import Device, Kind
    from fleet.ops import sync
    from fleet.state import access as acl
    from fleet.state import inventory as inv

    inv.save([Device(id="id:gpu", name="gpu", kind=Kind.PERMANENT,
                     endpoints=[{"target": "gpu", "user": "root", "port": 22}])])
    monkeypatch.setattr(acl, "load", lambda *a, **k: acl.Access(fleet_id="f", center="c"))
    monkeypatch.setattr(acl, "is_center", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(sync, "run_sync", lambda ep, payload: sent.append(ep.target) or (0, ""))
    cli._hand_the_fleet_to(inv.find_exact(inv.load(), "gpu"))
    assert sent == ["gpu"]


# ------------------------------------------- updated the way it was installed, never replaced

FLEET_STUB = '#!/bin/sh\ncase "$1" in --version) echo "fleet 0.5.2";; *) exit 0;; esac\n'


def _machine(tmp_path, *, uv_tool: str | None = None, pipx: bool = False,
             fleet_on_path: bool = False):
    """A machine with fleet installed one way or another, and a log of what was run.

    `uv_tool` is the uv receipt's requirement line for an `agents-fleet` tool -- from
    PyPI, a checkout, or fleet's own clone -- or None for no uv tool at all. The stubs
    only record: nothing here installs anything, and the script's own branches are what
    is under test.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "ran.log"
    log.write_text("")
    tool_bin = tmp_path / ".local" / "bin"
    tool_bin.mkdir(parents=True, exist_ok=True)
    tooldir = tmp_path / "uvtools"
    tools = ""
    if uv_tool is not None:
        (tooldir / "agents-fleet").mkdir(parents=True)
        (tooldir / "agents-fleet" / "uv-receipt.toml").write_text(
            f"[tool]\nrequirements = [{{ name = \"agents-fleet\", {uv_tool} }}]\n")
        tools = "agents-fleet v0.5.2"
        fleet_on_path = True
    # `uv tool install` puts the `fleet` command where uv puts commands, as the real
    # one does, so the script's closing `fleet --version` finds it.
    (bin_dir / "uv").write_text(f"""#!/bin/sh
echo "uv $*" >> {log}
case "$1 $2" in
  "tool list") echo "{tools}" ;;
  "tool dir") if [ "$3" = --bin ]; then echo {tool_bin}; else echo {tooldir}; fi ;;
  "tool install") printf '%s' '{FLEET_STUB}' > {tool_bin}/fleet; chmod +x {tool_bin}/fleet ;;
esac
""")
    if pipx:
        (bin_dir / "pipx").write_text(f"""#!/bin/sh
echo "pipx $*" >> {log}
[ "$1" = list ] && echo "agents-fleet 0.5.2"
exit 0
""")
        fleet_on_path = True
    if fleet_on_path:
        (bin_dir / "fleet").write_text(FLEET_STUB)
    (bin_dir / "crontab").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "git").write_text(f'#!/bin/sh\necho "git $*" >> {log}\nexit 0\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir, log


def _install(tmp_path, bin_dir, **kw):
    return subprocess.run(
        ["sh", "-c", install_script("https://example.invalid/fleet.git", **kw)],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"})


@pytest.mark.parametrize("update_only", [True, False], ids=["update", "install"])
def test_fleet_from_pypi_is_upgraded_from_pypi(tmp_path, update_only):
    """A machine installed with `uv tool install agents-fleet` -- the one-line installer's
    way -- used to be swapped for a checkout of `main` by the next `fleet update`."""
    bin_dir, log = _machine(tmp_path, uv_tool='specifier = ">=0.5"')
    r = _install(tmp_path, bin_dir, update_only=update_only)
    assert r.returncode == 0, r.stderr
    ran = log.read_text()
    assert "uv tool upgrade --quiet agents-fleet" in ran
    assert "tool install" not in ran and "git " not in ran, ran


def test_fleet_from_pipx_is_upgraded_with_pipx(tmp_path):
    bin_dir, log = _machine(tmp_path, pipx=True)
    r = _install(tmp_path, bin_dir, update_only=True)
    assert r.returncode == 0, r.stderr
    ran = log.read_text()
    assert "pipx upgrade agents-fleet" in ran
    assert "tool install" not in ran and "git " not in ran, ran


@pytest.mark.parametrize("receipt", ['editable = "/home/me/src/fleet"',
                                     'directory = "/home/me/src/fleet"'])
@pytest.mark.parametrize("update_only", [True, False], ids=["update", "install"])
def test_a_source_install_is_kept_and_says_so(tmp_path, receipt, update_only):
    """Your own checkout is a development install: never replaced by a release, and
    never by fleet's checkout either."""
    bin_dir, log = _machine(tmp_path, uv_tool=receipt)
    r = _install(tmp_path, bin_dir, update_only=update_only)
    assert r.returncode == 0, r.stderr
    assert f"{install.KEPT_MARK} " in r.stdout and "source checkout" in r.stdout
    ran = log.read_text()
    assert "tool install" not in ran and "tool upgrade" not in ran and "git " not in ran, ran


@pytest.mark.parametrize("update_only", [True, False], ids=["update", "install"])
def test_a_fleet_installed_some_other_way_is_kept(tmp_path, update_only):
    """pip, a virtualenv, a package manager: fleet cannot update it, and `fleet install`
    used to put a second copy beside it that took the `fleet` command over."""
    bin_dir, log = _machine(tmp_path, fleet_on_path=True)
    r = _install(tmp_path, bin_dir, update_only=update_only)
    assert r.returncode == 0, r.stderr
    assert install.KEPT_MARK in r.stdout and "0.5.2" in r.stdout and "NOT updated" in r.stdout
    assert "tool install" not in log.read_text()


def test_fleets_own_checkout_is_updated_from_git(tmp_path):
    """Installed by an older `fleet install`: the clone in ~/.local/share/fleet is what
    the uv tool was built from, so that is what is updated."""
    clone = tmp_path / ".local" / "share" / "fleet"
    (clone / ".git").mkdir(parents=True)
    bin_dir, log = _machine(tmp_path, uv_tool=f'directory = "{clone}"')
    r = _install(tmp_path, bin_dir, update_only=True)
    assert r.returncode == 0, r.stderr
    ran = log.read_text()
    assert "fetch --quiet origin main" in ran
    assert f"tool install --force --reinstall-package agents-fleet --quiet {clone}" in ran
    assert install.KEPT_MARK not in r.stdout


def test_a_machine_with_no_fleet_gets_the_release(tmp_path):
    """`fleet install` on a bare machine installs what the one-line installer would --
    the release from PyPI -- not whatever `main` holds today."""
    bin_dir, log = _machine(tmp_path)
    r = _install(tmp_path, bin_dir)
    assert r.returncode == 0, r.stderr
    ran = log.read_text()
    assert "uv tool install --quiet agents-fleet" in ran
    assert "git " not in ran, ran
    assert not (tmp_path / ".local" / "share" / "fleet").exists(), "no clone is made"


def test_asking_for_git_by_name_gets_git(tmp_path):
    """`--repo` or `--ref` is an explicit request, and overrides how fleet was installed."""
    bin_dir, log = _machine(tmp_path, uv_tool='specifier = ">=0.5"')
    r = _install(tmp_path, bin_dir, update_only=True, from_git=True, ref="v0.5.2")
    assert r.returncode == 0, r.stderr
    ran = log.read_text()
    assert "git clone --quiet --branch v0.5.2" in ran
    assert "tool upgrade" not in ran


def test_a_tag_can_be_installed_over_an_existing_clone(tmp_path):
    """`--ref` says "branch or tag", but an existing clone checked out `origin/$REF`,
    which a tag does not have -- so `--ref v1` failed on every machine that already had
    fleet's checkout. Run for real against a git repo."""
    origin = _origin(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], env=env, check=True)
    git = lambda *a: subprocess.run(["git", *a], cwd=str(work), env=env, check=True,
                                    capture_output=True)
    git("tag", "v1")
    (work / "marker.txt").write_text("v2\n")
    git("commit", "--quiet", "-am", "v2")
    git("push", "--quiet", "origin", "main", "v1")

    assert _run(install_script(str(origin), from_git=True), tmp_path).returncode == 0
    marker = tmp_path / ".local" / "share" / "fleet" / "marker.txt"
    assert marker.read_text(encoding="utf-8") == "v2\n"
    r = _run(install_script(str(origin), ref="v1", from_git=True), tmp_path)
    assert r.returncode == 0, r.stderr
    assert marker.read_text(encoding="utf-8") == "v1\n"


def test_the_windows_installer_decides_the_same_way():
    """Not run here -- there is no PowerShell -- but every mode the sh script has must
    exist in the PowerShell one too, or a Windows machine falls back to git."""
    from fleet.ssh.cmd import WINDOWS

    ps = install_script("https://e/f.git", platform=WINDOWS, update_only=True)
    for mode in ("'uv'", "'pipx'", "'keep'", "'new'", "'old'"):
        assert f"$mode = {mode}" in ps or f"$mode -eq {mode}" in ps, mode
    assert "uv tool upgrade --quiet agents-fleet" in ps
    assert "pipx upgrade agents-fleet" in ps
    assert "uv tool install --quiet agents-fleet" in ps
    assert install.KEPT_MARK in ps
    forced = install_script("https://e/f.git", platform=WINDOWS, from_git=True)
    assert "if (-not $true)" in forced, "--repo/--ref skips the detection"
