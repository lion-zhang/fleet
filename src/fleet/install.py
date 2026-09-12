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


def install_script(repo: str, *, ref: str = "main") -> str:
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

uv tool install --force --quiet "$DIR"
PATH="$HOME/.local/bin:$PATH" fleet --version
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
