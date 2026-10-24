"""install.sh itself, run for real in an empty home with nothing on PATH.

What it checks before installing anything, and the order it replaces an old fleet in: a
wrong order there left a machine with no fleet at all.
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


@pytest.mark.parametrize("how", ["env", "--join CODE", "--join="])
def test_an_old_join_line_still_installs_and_says_it_is_not_needed(tmp_path, how):
    """fleet 0.5's invite line carried a join code. Pasted today, it must still install
    fleet -- with a word that the code is not needed -- not fail on an unknown option."""
    if how == "env":
        p = run(tmp_path, join="fleet1:abc")
    elif how == "--join=":
        p = run(tmp_path, "--join=fleet1:abc")
    else:
        p = run(tmp_path, "--join", "fleet1:abc")
    assert "no longer needed" in p.stdout
    assert "unknown option" not in p.stderr
    assert "installing fleet" in p.stdout              # it got as far as the install


def test_a_plain_install_mentions_no_modes(tmp_path):
    p = run(tmp_path)
    assert "mode:" not in p.stdout and "center" not in p.stdout
    assert "installing fleet" in p.stdout


def test_an_unknown_option_is_refused(tmp_path):
    p = run(tmp_path, "--bogus")
    assert p.returncode != 0 and "unknown option" in p.stderr
    assert left_behind(tmp_path) == []


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
