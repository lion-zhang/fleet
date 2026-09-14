"""Install fleet itself on a device, promoting it from a probe target to infrastructure.

Everything else fleet touches needs nothing installed -- the probe is a POSIX sh script
piped over one connection, which is what makes onboarding a single pasted command. This
is the deliberate exception: a backup node has to run fleet, so fleet has to be there.

The device clones the repo over a forwarded SSH agent by default, so it authenticates to
GitHub as you and no credential is left behind on a machine you may not fully control.
Agent forwarding does let root on that box use your agent for as long as you are
connected, so it can be declined for a host you do not trust with that.
"""

from __future__ import annotations

import shlex

from .ssh.cmd import WINDOWS, Endpoint

INSTALL_DIR = "$HOME/.local/share/fleet"
WINDOWS_INSTALL_DIR = "$env:USERPROFILE\\.local\\share\\fleet"


def _posix_script(repo: str, *, ref: str = "main") -> str:
    """The sh run on the device. Idempotent: `fleet install` doubles as `fleet update`.

    uv is fetched when missing because it also solves the Python problem -- fleet needs
    3.12 and most servers ship something older, and uv will fetch its own interpreter
    rather than requiring one to be present.

    Earlier versions left a cron entry here so an idle broker kept syncing itself. Sync
    is center-initiated now -- the center dials out and nothing connects to it -- so that
    entry can only fail, silently, forever. Installing removes it.
    """
    return f"""set -e
REPO={shlex.quote(repo)}
REF={shlex.quote(ref)}
DIR="{INSTALL_DIR}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || exit 90
  if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --quiet origin "$REF"
  git -C "$DIR" checkout --quiet -B "$REF" "origin/$REF"
else
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet --branch "$REF" "$REPO" "$DIR"
fi

# Stop the center's service before replacing the files it is running from. Not
# politeness: on Windows a live fleet.exe holds its own installation open and the install
# fails against it with an error that mentions nothing about why. Quiet and best-effort,
# because a machine that is not a center has nothing to stop.
PATH="$HOME/.local/bin:$PATH" fleet service stop >/dev/null 2>&1 || true

# --reinstall-package, not --reinstall: the problem is uv reusing a cached build of
# fleet when the version has not changed, and rebuilding every dependency to fix that
# turns a deploy into a download of the world.
uv tool install --force --reinstall-package fleet-broker --quiet "$DIR"

# Put fleet on the PATH of a terminal the user opens later, not just this script's.
# Without it fleet installs correctly and then is not there when they type its name --
# the shim lands in a directory nothing has ever added to PATH. uv owns that directory,
# so uv is asked to do it; it is idempotent, and it is the step the user should not have
# to find out about afterwards.
uv tool update-shell >/dev/null 2>&1 || true

PATH="$HOME/.local/bin:$PATH" fleet --version

# Back up if it was there. `install` is idempotent, so this must not start a service on a
# machine that never had one -- `service start` does nothing when none is installed.
PATH="$HOME/.local/bin:$PATH" fleet service start >/dev/null 2>&1 || true
{_drop_timer_block()}"""


def _drop_timer_block() -> str:
    """Remove the cron entry earlier versions installed.

    It ran `fleet sync` on a timer, which dialled the center. Sync is center-initiated
    now -- nothing connects to the center at all -- so the entry can only fail, silently,
    every ten minutes forever. Filtered by the same marker it was written with, so the
    user's own entries are untouched.
    """
    marker = "# fleet-sync"
    # Read before writing: piping `crontab -l` straight into `crontab -` runs both ends
    # concurrently, and the writer can truncate the table before the reader has seen it.
    return f"""
if command -v crontab >/dev/null 2>&1; then
  fleet_cron_rest=$(crontab -l 2>/dev/null | grep -v {shlex.quote(marker)} || true)
  printf '%s\\n' "$fleet_cron_rest" | crontab - || true
fi
"""


def build_install_argv(ep: Endpoint, *, forward_agent: bool = True,
                       connect_timeout: int = 15, platform: str = "") -> list[str]:
    """ssh invocation for the installer.

    Unlike the probe this is interactive-ish and long-running: it may fetch uv and clone
    a repo, so no BatchMode timeout games. -A forwards the agent so the clone can
    authenticate as you without a credential at rest.
    """
    argv = ["ssh", "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if forward_agent:
        argv.append("-A")
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.identity:
        argv += ["-i", ep.identity]
    if ep.jump:
        argv += ["-J", ep.jump]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    # Both read the script from stdin, so nothing long or quoted has to survive a second
    # round of shell parsing on the way in.
    argv.append("powershell -NoProfile -Command -" if platform == WINDOWS else "sh -s")
    return argv


def _windows_script(repo: str, ref: str = "main") -> str:
    """The PowerShell run on a Windows device. Same contract as the sh one.

    Written out rather than shimmed through Git Bash. `sh.exe` usually exists on a
    Windows box with git installed, and the POSIX script very nearly runs under it --
    which is the trap: `$HOME` becomes an MSYS path that uv may or may not translate,
    and the failure is a half-finished install rather than a refusal.

    Everything the POSIX script explains applies here: uv is fetched when missing because
    it also solves the Python problem, the service is stopped before its files are
    replaced (on Windows a live fleet holds its own installation open and the install
    fails against it saying nothing about why), and --reinstall-package rather than
    --reinstall because the problem is a cached build of fleet, not of the world.

    No cron block: that cleanup is for machines that had one, and Windows never did.
    """
    repo_lit = repo.replace("'", "''")
    ref_lit = ref.replace("'", "''")
    return f"""$ErrorActionPreference = 'Stop'
$repo = '{repo_lit}'
$ref  = '{ref_lit}'
$dir  = "{WINDOWS_INSTALL_DIR}"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {{
  try {{ irm https://astral.sh/uv/install.ps1 | iex }} catch {{ exit 90 }}
  $env:PATH = "$env:USERPROFILE\\.local\\bin;$env:PATH"
}}

if (Test-Path (Join-Path $dir '.git')) {{
  git -C $dir fetch --quiet origin $ref
  git -C $dir checkout --quiet -B $ref "origin/$ref"
}} else {{
  New-Item -ItemType Directory -Force -Path (Split-Path $dir) | Out-Null
  git clone --quiet --branch $ref $repo $dir
}}

# Before replacing the files it is running from. Best-effort and quiet: a machine that is
# not a center has no service to stop.
$fleet = Join-Path $env:USERPROFILE '.local\\bin\\fleet.exe'
if (Test-Path $fleet) {{ & $fleet service stop 2>&1 | Out-Null }}

uv tool install --force --reinstall-package fleet-broker --quiet $dir

# So `fleet` works in a terminal the user opens later, not just in this script. Without
# it the shim lands in a directory nothing has ever added to PATH, and fleet installs
# correctly and then is not there when they type its name.
uv tool update-shell 2>&1 | Out-Null

& $fleet --version
if (Test-Path $fleet) {{ & $fleet service start 2>&1 | Out-Null }}
"""


def install_script(repo: str, *, ref: str = "main", platform: str = "") -> str:
    """The installer for whichever shell the far side speaks.

    `platform` comes from the last probe via `remote_platform`, which is the one place
    that decides. An unprobed machine reads POSIX, which is the safe way to be wrong: a
    POSIX script on Windows fails loudly, where the reverse can appear to succeed.
    """
    if platform == WINDOWS:
        return _windows_script(repo, ref)
    return _posix_script(repo, ref=ref)
