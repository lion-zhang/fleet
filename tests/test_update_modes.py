"""`fleet update` updates fleet the way it was installed -- the rule the installer
keeps, that an existing core is never replaced, holds for updates too.

Found in review: the update script cloned `main` and reinstalled from it everywhere, so
a machine installed from PyPI got whatever the branch held that day, and your own
checkout was replaced. And `--ref TAG` failed on any machine that already had a clone."""

from __future__ import annotations

import subprocess
import sys

import pytest

from fleet.install import install_script

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")


def _sandbox(tmp_path, *, receipt: str | None, pipx: bool = False):
    """Stubs for uv, pipx, fleet and crontab. `receipt` is uv's record of how fleet was
    installed; None means uv has no fleet tool."""
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "calls.log"
    tooldir = tmp_path / "tools"
    if receipt is not None:
        (tooldir / "agents-fleet").mkdir(parents=True)
        (tooldir / "agents-fleet" / "uv-receipt.toml").write_text(receipt, encoding="utf-8")
    listing = "agents-fleet v0.5.0\\n- fleet" if receipt is not None else ""
    (b / "uv").write_text(
        f'#!/bin/sh\necho "uv $@" >> {log}\n'
        f'case "$1 $2" in "tool list") printf "{listing}\\n";; "tool dir") echo {tooldir};; esac\n')
    (b / "pipx").write_text(
        f'#!/bin/sh\necho "pipx $@" >> {log}\n'
        + ('[ "$1" = list ] && echo "agents-fleet 0.5.0"\nexit 0\n' if pipx else 'exit 1\n'))
    (b / "fleet").write_text(f'#!/bin/sh\necho "fleet $@" >> {log}\n'
                             '[ "$1" = --version ] && echo "fleet 0.5.0"\nexit 0\n')
    (b / "crontab").write_text("#!/bin/sh\nexit 0\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b, log


def _run(tmp_path, b, **kw):
    script = install_script("https://example.invalid/fleet.git", update_only=True, **kw)
    p = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60,
                       env={"HOME": str(tmp_path), "PATH": f"{b}:/usr/bin:/bin"})
    return p, (tmp_path / "calls.log").read_text(encoding="utf-8")


def test_a_pypi_install_is_upgraded_in_place(tmp_path):
    b, _ = _sandbox(tmp_path, receipt='[tool]\nrequirements = [{ name = "agents-fleet" }]\n')
    p, calls = _run(tmp_path, b)
    assert p.returncode == 0, p.stderr
    assert "uv tool upgrade --quiet agents-fleet" in calls
    assert "tool install" not in calls
    assert not (tmp_path / ".local" / "share" / "fleet").exists(), "nothing cloned"


def test_a_source_install_of_yours_is_kept(tmp_path):
    b, _ = _sandbox(tmp_path, receipt='[tool]\nrequirements = [{ name = "agents-fleet", '
                                      'editable = "/home/me/src/fleet" }]\n')
    p, calls = _run(tmp_path, b)
    assert p.returncode == 0, p.stderr
    assert "kept as it is" in p.stdout
    assert "upgrade" not in calls and "tool install" not in calls


def test_a_pipx_install_is_upgraded_with_pipx(tmp_path):
    b, _ = _sandbox(tmp_path, receipt=None, pipx=True)
    p, calls = _run(tmp_path, b)
    assert p.returncode == 0, p.stderr
    assert "pipx upgrade agents-fleet" in calls and "tool install" not in calls


def test_fleet_installed_some_other_way_is_kept(tmp_path):
    b, _ = _sandbox(tmp_path, receipt=None)
    p, calls = _run(tmp_path, b)
    assert p.returncode == 0, p.stderr
    assert "installed another way" in p.stdout and "tool install" not in calls


def _origin(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, env=env, check=True,  # noqa: E731
                                              capture_output=True)
    run("git", "init", "--quiet", "--bare", "-b", "main", str(origin))
    run("git", "clone", "--quiet", str(origin), str(work))
    (work / "marker.txt").write_text("v1\n")
    run("git", "add", "-A", cwd=work)
    run("git", "commit", "--quiet", "-m", "v1", cwd=work)
    run("git", "tag", "v1.0", cwd=work)
    (work / "marker.txt").write_text("v2\n")
    run("git", "commit", "--quiet", "-am", "v2", cwd=work)
    run("git", "push", "--quiet", "--tags", "origin", "main", cwd=work)
    return origin


def test_a_tag_works_on_a_machine_that_already_has_a_clone(tmp_path):
    origin = _origin(tmp_path)
    b, _ = _sandbox(tmp_path, receipt=None)
    env = {"HOME": str(tmp_path), "PATH": f"{b}:/usr/bin:/bin"}
    first = install_script(str(origin))                            # a clone of main
    assert subprocess.run(["sh", "-c", first], env=env, capture_output=True).returncode == 0
    tagged = install_script(str(origin), ref="v1.0", update_only=True, from_git=True)
    p = subprocess.run(["sh", "-c", tagged], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / ".local/share/fleet/marker.txt").read_text() == "v1\n"
