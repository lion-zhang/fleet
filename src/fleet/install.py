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

from .sshcmd import Endpoint

INSTALL_DIR = "$HOME/.local/share/fleet"


def install_script(repo: str, *, ref: str = "main", timer_minutes: int = 10) -> str:
    """The sh run on the device. Idempotent: `fleet install` doubles as `fleet update`.

    uv is fetched when missing because it also solves the Python problem -- fleet needs
    3.12 and most servers ship something older, and uv will fetch its own interpreter
    rather than requiring one to be present.

    A cron entry keeps an idle broker syncing. Opportunistic sync only fires when a
    command runs, and a backup node may go weeks without one -- a replica that quietly
    stopped replicating is worse than no replica, because you would still count on it.
    cron rather than a systemd user unit: it is present on far more servers and needs no
    lingering enabled to run without a login session. `timer_minutes=0` skips it.
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

uv tool install --force --quiet "$DIR"
PATH="$HOME/.local/bin:$PATH" fleet --version
{_timer_block(timer_minutes)}"""


def _timer_block(minutes: int) -> str:
    """Idempotent cron entry. Filtered by a marker comment so re-installing replaces our
    line and leaves every other entry the user has untouched."""
    if minutes <= 0:
        return ""
    marker = "# fleet-sync"
    line = (f'*/{minutes} * * * * PATH="$HOME/.local/bin:$PATH" '
            f'fleet sync >/dev/null 2>&1 {marker}')
    # Read the existing table into a variable BEFORE writing. Piping `crontab -l`
    # straight into `crontab -` runs both ends concurrently, so the writer can truncate
    # the table before the reader has seen it -- and the user's own entries vanish.
    return f"""
if command -v crontab >/dev/null 2>&1; then
  fleet_cron_existing=$(crontab -l 2>/dev/null | grep -v {shlex.quote(marker)} || true)
  {{
    if [ -n "$fleet_cron_existing" ]; then printf '%s\\n' "$fleet_cron_existing"; fi
    echo {shlex.quote(line)}
  }} | crontab - || true
fi
"""


def build_install_argv(ep: Endpoint, *, forward_agent: bool = True,
                       connect_timeout: int = 15) -> list[str]:
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
    argv.append("sh -s")
    return argv
