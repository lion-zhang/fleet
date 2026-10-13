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


def _old_tool_world(tmp_path: Path, *, install_fails: bool) -> tuple[dict, Path, Path]:
    """A machine whose `fleet` is a uv tool from before the rename, and a uv that does
    what uv does to it: `tool uninstall` deletes the `fleet` command it once wrote."""
    home, bin_, tools = tmp_path / "home", tmp_path / "bin", tmp_path / "tools"
    for d in (home, bin_, tools):
        d.mkdir(exist_ok=True)
    log, gone = tmp_path / "uv.log", tmp_path / "old-gone"
    (tools / "fleet").write_text("#!/bin/sh\necho fleet 0.4.0\n")
    (tools / "fleet").chmod(0o755)
    (bin_ / "uv").write_text(f"""#!/bin/sh
echo "uv $*" >> {log}
case "$1 $2" in
  "tool list") [ -f {gone} ] || printf 'fleet-broker v0.4.0\\n- fleet\\n' ;;
  "tool dir") echo {tools} ;;
  "tool install") {'exit 1' if install_fails else ''}
    printf '#!/bin/sh\\necho fleet 0.5.1\\n' > {tools}/fleet; chmod +x {tools}/fleet ;;
  "tool uninstall") rm -f {tools}/fleet; touch {gone} ;;
esac
exit 0
""")
    (bin_ / "uv").chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin"}
    return env, log, tools / "fleet"


def test_an_old_tool_is_replaced_only_once_the_new_one_is_in(tmp_path):
    env, log, fleet = _old_tool_world(tmp_path, install_fails=False)
    p = subprocess.run([SH, str(REPO / "install.sh"), "--no-setup"], env=env,
                       capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines()
    first_install = next(i for i, c in enumerate(calls) if c.startswith("uv tool install"))
    uninstall = next(i for i, c in enumerate(calls) if c.startswith("uv tool uninstall"))
    assert first_install < uninstall, calls
    assert "--force" in calls[first_install]
    # and the `fleet` command the uninstall took with it is back
    assert fleet.exists() and "0.5.1" in fleet.read_text(), (p.stdout, p.stderr, calls)


def test_a_failed_install_leaves_the_old_fleet_working(tmp_path):
    """Uninstalled first, an install that then failed left no fleet on the machine."""
    env, log, fleet = _old_tool_world(tmp_path, install_fails=True)
    p = subprocess.run([SH, str(REPO / "install.sh"), "--no-setup"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    assert not any(c.startswith("uv tool uninstall") for c in log.read_text().splitlines())
    assert fleet.exists() and "0.4.0" in fleet.read_text()
