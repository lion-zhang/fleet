"""install.sh itself, run for real in an empty home with nothing on PATH.

The checks it makes before installing anything are the ones that decide what the machine
becomes, and a wrong one there is not undone by a re-run: a machine meant to join a fleet
that starts one of its own instead is the center of a second fleet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None or os.name == "nt", reason="needs a POSIX sh")


def run(tmp_path: Path, *args: str, join: str | None = None) -> subprocess.CompletedProcess:
    """install.sh with HOME and PATH that hold no fleet, and a uv that does nothing.

    The fake uv is there so a run that gets past the checks stops at its first real step
    instead of downloading anything.
    """
    home, bin_ = tmp_path / "home", tmp_path / "bin"
    home.mkdir(exist_ok=True)
    bin_.mkdir(exist_ok=True)
    uv = bin_ / "uv"
    uv.write_text("#!/bin/sh\nexit 1\n")
    uv.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin"}
    if join is not None:
        env["FLEET_JOIN"] = join
    return subprocess.run([SH, str(REPO / "install.sh"), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def left_behind(tmp_path: Path) -> list[str]:
    home = tmp_path / "home"
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


@pytest.mark.parametrize("how", ["env", "--join ''", "--join="])
def test_an_empty_join_code_is_refused_before_anything_is_installed(tmp_path, how):
    if how == "env":
        p = run(tmp_path, join="")
        name = "FLEET_JOIN"
    elif how == "--join=":
        p = run(tmp_path, "--join=")
        name = "--join"
    else:
        p = run(tmp_path, "--join", "")
        name = "--join"
    assert p.returncode != 0
    assert f"{name} is set but empty" in p.stderr
    assert "fleet invite" in p.stderr
    assert "mode: center" not in p.stdout          # it never got as far as choosing
    assert left_behind(tmp_path) == []             # no uv, no fleet, no fleet state


def test_no_join_code_at_all_still_starts_a_fleet_and_says_so_plainly(tmp_path):
    p = run(tmp_path)
    assert "mode: center -- starting a NEW fleet" in p.stdout
    assert "--join CODE" in p.stdout


def test_a_join_code_is_still_a_member_install(tmp_path):
    p = run(tmp_path, join="fleet1:abc")
    assert "mode: member" in p.stdout
    assert "set but empty" not in p.stderr


def test_something_that_is_not_a_code_is_still_refused(tmp_path):
    p = run(tmp_path, join="not-a-code")
    assert p.returncode != 0 and "not a fleet invite code" in p.stderr


def test_installing_the_command_only_needs_no_code(tmp_path):
    p = run(tmp_path, "--no-setup", join="")
    assert "set but empty" not in p.stderr
    assert "mode: none" in p.stdout
