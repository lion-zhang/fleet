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

from .ssh.cmd import WINDOWS, Endpoint, local_platform

INSTALL_DIR = "$HOME/.local/share/fleet"
WINDOWS_INSTALL_DIR = "$env:USERPROFILE\\.local\\share\\fleet"

# Exit codes the script reserves for itself, distinct from anything a shell or uv
# returns, so the caller can tell them apart from a failure.
NO_UV = 90                  # the device could not fetch uv -- probably no internet
NOTHING_TO_UPDATE = 91      # update-only, and this device has no fleet to update


def _posix_script(repo: str, *, ref: str = "main", update_only: bool = False) -> str:
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
{_skip_unless_installed(update_only)}
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || exit 90
  if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

if [ -d "$DIR/.git" ]; then
  # The repo we were given, not the one this clone happened to be made with. Without
  # this, `--repo` was silently ignored for every machine that already had fleet: the
  # fetch used whatever `origin` was set to years ago, so an install could not be pointed
  # at a new remote or moved from ssh to https, and failed with an auth error naming a
  # URL the caller never asked for.
  git -C "$DIR" remote set-url origin "$REPO"
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


def _skip_unless_installed(update_only: bool) -> str:
    """Leave a device that has no fleet alone, when we were asked to update rather than
    to install.

    `fleet update --all` reaches every device with a route, and every device that is only
    ever a probe target needs nothing installed -- that is the whole shape of the tool.
    Deploying a fix to the machines that run fleet therefore also installed it on a NAS,
    a rental and anything else that answered, which is not what the command says and not
    what anyone reaching for it wants.

    The check is here rather than in the caller because the caller cannot know: nothing
    in the inventory records whether fleet is installed, and a field that said so would
    be wrong the moment someone removed it by hand. The device is the authority.

    Placed before uv is fetched, so a skipped device is left exactly as it was found.
    """
    if not update_only:
        return ""
    # PATH is searched with ~/.local/bin added, because this script runs under a
    # non-interactive shell that has never read a profile -- the same reason
    # `remote_command` exports it. Without that, a machine running a perfectly good
    # uv-installed fleet looks bare and gets skipped.
    return f"""
fleet_on_path=$(PATH="$HOME/.local/bin:$PATH" sh -c 'command -v fleet' 2>/dev/null || true)
if [ ! -d "$DIR/.git" ] && [ -z "$fleet_on_path" ]; then
  exit {NOTHING_TO_UPDATE}
fi
"""


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
    #
    # Windows arrives base64 and is decoded on the far side. `powershell -Command -`
    # reads stdin and evaluates it *statement by statement*, so the first line of a
    # multi-line `if {` is a syntax error on its own and everything after it is quietly
    # skipped: the installer appears to run, says nothing, and does nothing. Measured,
    # not assumed -- a one-line `while` loop came back fine and an `if/else` block came
    # back empty. Decoding the whole thing first makes it one unit again.
    argv.append(WINDOWS_STDIN_SHELL if platform == WINDOWS else "sh -s")
    return argv


# The installer arrives on stdin and is written out before it is run, because neither
# shorter route survives a real Windows box.
#
# `powershell -Command -` reads stdin and evaluates it statement by statement, so the
# first line of a multi-line `if {` is a syntax error on its own and everything after it
# is quietly skipped: the installer appears to run, says nothing, and does nothing.
# Measured -- a one-line `while` came back fine and an `if/else` block came back empty.
#
# Base64 plus `iex` was the answer to that, and it is refused outright on a machine with
# Defender's script rules on: decoding into `Invoke-Expression` is the shape obfuscated
# malware has, so the whole command dies with a bare "Access is denied." before a line of
# it runs. Measured too, on this fleet's own center, which is how it was found.
#
# A file has neither problem. It is one unit, it is ordinary, and `-File` propagates the
# script's exit code, which is what carries NOTHING_TO_UPDATE back to the caller.
_WINDOWS_SPOOL = ("[Console]::In.ReadToEnd() | "
                  "Set-Content -LiteralPath $env:TEMP\\fleet-install.ps1")
# `-ExecutionPolicy Bypass` because the default on a Windows client is Restricted, which
# refuses a .ps1 from disk -- the one difference a file makes that we have to pay for.
_WINDOWS_RUN = "powershell -NoProfile -ExecutionPolicy Bypass -File {temp}\\fleet-install.ps1"

# Through cmd.exe, which is what sshd hands a command to: `%TEMP%`, and `&&` so the run
# only happens if the write did.
WINDOWS_STDIN_SHELL = (f'powershell -NoProfile -Command "{_WINDOWS_SPOOL}" '
                       f'&& {_WINDOWS_RUN.format(temp="%TEMP%")}')


def local_install_argv() -> list[str]:
    """How to run the installer on the machine we are standing on. The script arrives on
    stdin, as `payload_for(script, local_platform())` -- never as an argument.

    `sh -c "$script"` on a Windows center is the same trap as sending it the POSIX script
    over ssh: sh.exe is usually present because git is, the script very nearly runs, and
    what comes out is a half-finished install rather than a refusal.

    Not `local_shell_argv()`, which is `powershell -Command -` on Windows: that reads
    stdin and evaluates it statement by statement, so the first line of a multi-line
    `if {` is a syntax error on its own and everything after it is skipped silently.
    Decoding the whole payload first makes it one unit again, exactly as the remote
    installer does.
    """
    if local_platform() == WINDOWS:
        # No cmd.exe in the way here, so the two halves are one -Command and the exit
        # code is passed on by hand.
        return ["powershell", "-NoProfile", "-Command",
                f"{_WINDOWS_SPOOL}; {_WINDOWS_RUN.format(temp='$env:TEMP')}; "
                "exit $LASTEXITCODE"]
    return ["sh", "-s"]


def payload_for(script: str, platform: str = "") -> bytes:
    """What to write to the installer's stdin. The same bytes for either shell: Windows
    spools them to a file rather than decoding them (see `_WINDOWS_SPOOL`).

    Bytes, never text. `text=True` wraps stdin in a TextIOWrapper with newline=None,
    which rewrites every \n to \r\n on Windows -- the bug that made a POSIX center send
    payload.sh verbatim and a Windows one send it CRLF.

    `platform` is kept because every caller has one to hand and a delivery that stops
    depending on it should not be a reason to go and edit them all.
    """
    return script.encode()


def _windows_script(repo: str, ref: str = "main", *, update_only: bool = False) -> str:
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
{_skip_unless_installed_ps(update_only)}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {{
  try {{ irm https://astral.sh/uv/install.ps1 | iex }} catch {{ exit 90 }}
  $env:PATH = "$env:USERPROFILE\\.local\\bin;$env:PATH"
}}

if (Test-Path (Join-Path $dir '.git')) {{
  # The repo we were given, not the one this clone happened to be made with.
  git -C $dir remote set-url origin $repo
  git -C $dir fetch --quiet origin $ref
  git -C $dir checkout --quiet -B $ref "origin/$ref"
}} else {{
  New-Item -ItemType Directory -Force -Path (Split-Path $dir) | Out-Null
  git clone --quiet --branch $ref $repo $dir
}}

# Stop the center before replacing the files it runs from -- and do it here rather than
# by calling `fleet service stop`, which was the mistake this replaces. `fleet.exe` is a
# uv trampoline that executes Scripts\\python.exe, so stopping fleet *by running fleet*
# holds open the directory uv is about to remove. It fails with "Access is denied" on
# Scripts, and that is not a failed update: uv has deleted most of the installation by
# then, so the machine is left with no working fleet at all.
# try/catch, and not because either is allowed to fail quietly for its own sake. A
# native command that writes to stderr becomes an error record, and under
# `$ErrorActionPreference = 'Stop'` that record is terminating -- so "ERROR: The process
# fleet.exe not found", which is the normal answer on a machine whose service is not
# running, aborted the installer before it reached uv. Redirecting the stream away does
# not help on Windows PowerShell 5: the record is raised either way. Measured on the
# center, which could not be updated at all until this was written like this.
try {{ schtasks /end /tn fleet-center 2>&1 | Out-Null }} catch {{ }}
try {{ taskkill /f /im fleet.exe 2>&1 | Out-Null }} catch {{ }}
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object {{ $_.CommandLine -like '*-m*fleet*center*--listen*' }} |
  ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}

# Ending a process returns before Windows has released its files, so wait for it rather
# than racing it.
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {{
  $alive = @(Get-Process fleet -ErrorAction SilentlyContinue) + @(
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object {{ $_.CommandLine -like '*-m*fleet*center*--listen*' }})
  if ($alive.Count -eq 0) {{ break }}
  Start-Sleep -Milliseconds 400
}}

$fleet = Join-Path $env:USERPROFILE '.local\\bin\\fleet.exe'

uv tool install --force --reinstall-package fleet-broker --quiet $dir

# So `fleet` works in a terminal the user opens later, not just in this script. Without
# it the shim lands in a directory nothing has ever added to PATH, and fleet installs
# correctly and then is not there when they type its name.
try {{ uv tool update-shell 2>&1 | Out-Null }} catch {{ }}

& $fleet --version
if (Test-Path $fleet) {{
  try {{ & $fleet service start 2>&1 | Out-Null }} catch {{ }}
}}
"""


def _skip_unless_installed_ps(update_only: bool) -> str:
    """The PowerShell half of `_skip_unless_installed`, with its reasoning.

    `fleet.exe` is checked by path as well as by `Get-Command`, because uv's shim lands
    in a directory this session may not have on PATH -- which is the very thing
    `uv tool update-shell` exists to fix for the terminal you open afterwards.
    """
    if not update_only:
        return ""
    shim = "$env:USERPROFILE\\.local\\bin\\fleet.exe"
    return f"""
if (-not (Test-Path (Join-Path $dir '.git')) -and
    -not (Get-Command fleet -ErrorAction SilentlyContinue) -and
    -not (Test-Path "{shim}")) {{ exit {NOTHING_TO_UPDATE} }}
"""


def install_script(repo: str, *, ref: str = "main", platform: str = "",
                   update_only: bool = False) -> str:
    """The installer for whichever shell the far side speaks.

    `platform` comes from the last probe via `remote_platform`, which is the one place
    that decides. An unprobed machine reads POSIX, which is the safe way to be wrong: a
    POSIX script on Windows fails loudly, where the reverse can appear to succeed.
    """
    if platform == WINDOWS:
        return _windows_script(repo, ref, update_only=update_only)
    return _posix_script(repo, ref=ref, update_only=update_only)
