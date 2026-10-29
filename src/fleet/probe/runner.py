"""Execute the probe over SSH, in parallel, with hard deadlines.

Two invariants this module exists to guarantee:
  1. A device that fails is recorded as failed and EVERY other device still returns.
  2. An ssh that hangs is always killed. `timeout(1)` is not present on macOS, so the
     deadline is enforced in Python -- and by killing the process GROUP, because killing
     only the ssh pid leaves the ControlMaster child alive holding the socket.
"""

from __future__ import annotations

import concurrent.futures
import os
import signal
import base64
import re
import shlex
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from ..models import ProbeResult, Snapshot, Status
from ..ssh.cmd import (IS_WINDOWS, WINDOWS, Endpoint, build_argv, jump_hops,
                      local_platform, local_shell_argv)
from .parse import MissingSentinel, parse_payload

PAYLOAD = Path(__file__).with_name("payload.sh")
PAYLOAD_PS1 = Path(__file__).with_name("payload.ps1")

# What cmd.exe says when handed `sh -s`. The host is up and the key worked; it
# simply has no POSIX shell, so the same probe is retried in PowerShell.
_NO_POSIX_SHELL = ("is not recognized as an internal or external command",
                   "operable program or batch file",
                   # cmd.exe's answer to the probe's own remote command, which spools the
                   # script to a temp file before running it. It never reaches the part
                   # that would name a missing program, so it says only this -- and says
                   # it while exiting 0, which is why the retry has to look at a probe
                   # that "succeeded" with nothing in it.
                   "the system cannot find the path specified")
# What the classifier turns those into. Matched as well as the raw text, because
# classification runs first and would otherwise replace the only signal the retry has --
# which it did, silently, the first time this was wired up.
WINDOWS_DETAIL = "host is Windows: no POSIX shell for the probe payload"
BANNER_DETAIL = "the port answers, but never speaks ssh"

# stderr signature -> status. Order matters: the first match wins.
_ERROR_SIGNATURES: tuple[tuple[str, Status, str], ...] = (
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", Status.HOST_KEY_MISMATCH, "host key changed"),
    ("Host key verification failed", Status.HOST_KEY_MISMATCH, "host key rejected"),
    ("Permission denied", Status.AUTH_FAILED, "credentials rejected"),
    ("Too many authentication failures", Status.AUTH_FAILED, "too many auth attempts"),
    ("no matching host key", Status.AUTH_FAILED, "no mutually supported host key"),
    ("Connection refused", Status.REFUSED, "nothing listening on that port"),
    ("Connection closed by", Status.CLOSED, "peer closed the connection"),
    ("Connection reset by", Status.CLOSED, "peer reset the connection"),
    # Before the plain timeouts it contains: the port took the connection, so the
    # machine is there -- "nothing answered at that address" sent people to check it was
    # switched on, when something else holds the port or its sshd is wedged.
    ("timed out during banner exchange", Status.TIMEOUT, BANNER_DETAIL),
    ("Operation timed out", Status.TIMEOUT, "network timeout"),
    ("Connection timed out", Status.TIMEOUT, "network timeout"),
    ("Could not resolve hostname", Status.UNREACHABLE, "DNS lookup failed"),
    ("Name or service not known", Status.UNREACHABLE, "DNS lookup failed"),
    ("No route to host", Status.UNREACHABLE, "no route"),
    ("Network is unreachable", Status.UNREACHABLE, "network unreachable"),
    # cmd.exe answering our `sh -s`. The host is up and our key worked -- it simply has
    # no POSIX shell. Without this the failure arrives as the tail of a Windows error
    # ("operable program or batch file.") which says nothing about what to do.
    ("is not recognized as an internal or external command", Status.PROBE_ERROR,
     WINDOWS_DETAIL),
    ("operable program or batch file", Status.PROBE_ERROR, WINDOWS_DETAIL),
)


# Lines OpenSSH emits that say nothing about reachability. Scrubbed before classifying
# so they can never mask a real diagnostic, and before storing so they do not fill the UI.
_BENIGN_STDERR = (
    "post-quantum",                 # OpenSSH 10.x key-exchange advisory
    "Permanently added",            # accept-new recorded a first-contact host key
    "Warning: Permanently added",
    "debug1:",
    "Authenticated to",
    "Connection to ",
    # The far end of a jump host's stdio forward, after any failure through it. It
    # matched "Connection closed by" ahead of what actually went wrong: a jump host whose
    # name did not resolve, or a machine its jump host had never heard of, read "closed".
    "Connection closed by UNKNOWN port",
)


def scrub_stderr(stderr: str) -> str:
    keep = [ln for ln in stderr.splitlines()
            if ln.strip() and not any(b in ln for b in _BENIGN_STDERR)]
    return "\n".join(keep)


# ssh's own line when the TCP connection itself failed, before any server said anything,
# and the reasons the signatures above already tell apart.
_CONNECT_FAILED = re.compile(r"connect to host \S+ port \d+: (.+)")
_CONNECT_REASONS = ("Connection refused", "timed out", "No route to host",
                    "Network is unreachable", "Name or service not known")


def classify_stderr(stderr: str) -> tuple[Status, str]:
    cleaned = scrub_stderr(stderr)
    if (m := _CONNECT_FAILED.search(cleaned)) and not any(
            reason in m.group(1) for reason in _CONNECT_REASONS):
        # Nothing answered, whatever the reason says. "Permission denied" from a local
        # firewall or sandbox read as the machine rejecting the key, and a reason with no
        # signature of its own ("failure", with no IPv6 here) as "it answered, but the
        # probe failed".
        return Status.UNREACHABLE, f"could not connect: {m.group(1).strip()[:160]}"
    for needle, status, detail in _ERROR_SIGNATURES:
        if needle in cleaned:
            return status, detail
    last = (cleaned.strip().splitlines() or [""])[-1][:200]
    if not last:
        # ssh failed but said nothing at all -- almost always the peer hanging up during
        # banner exchange, which is what a recycled rental port looks like.
        return Status.CLOSED, "peer closed the connection without a diagnostic"
    return Status.PROBE_ERROR, last


JUMP_DETAIL = "the jump host"


def through_jump(ep: Endpoint, stderr: str, detail: str) -> str:
    """The detail of a failure that happened at a jump host, said of the jump host.

    ssh reports a jump host's failures in the same words as the machine's, so a jump
    host whose name did not resolve, that refused, or that turned the key away read as
    the machine itself being down or saying no -- and doctor sent people to enable sshd
    on a machine nothing had reached. The jump host's own name in what ssh printed is
    what tells them apart: a failure behind it comes back as `channel 0: open failed`."""
    if not ep.jump or "channel 0: open failed" in stderr:
        return detail
    cleaned = scrub_stderr(stderr)
    for hop in jump_hops(ep.jump):
        if hop.host == ep.target and hop.port == ep.port and hop.user == ep.user:
            continue                        # one name for both: nothing tells them apart
        h = re.escape(hop.host)
        if re.search(rf"Could not resolve hostname {h}:", cleaned):
            return f"{JUMP_DETAIL} {hop.shown} has no address: DNS lookup failed"
        if m := re.search(rf"connect to host {h} port {hop.port}: (.+)", cleaned):
            return f"{JUMP_DETAIL} {hop.shown} did not answer: {m.group(1).strip()}"
        for m in re.finditer(rf"(\S+)@{h}: Permission denied", cleaned):
            if hop.host != ep.target or (hop.user and m.group(1) == hop.user != ep.user):
                return f"{JUMP_DETAIL} {hop.shown} did not let us in: credentials rejected"
    if "timed out during banner exchange" in cleaned:
        hops = jump_hops(ep.jump)
        return f"{detail}: the jump host {hops[0].shown}, or the machine behind it"
    return detail


def _kill(proc: subprocess.Popen) -> None:
    """Kill the whole process group; the ssh child would otherwise survive."""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def probe_env(mode: str, disk_paths: list[str] | None = None, *, light: bool = False,
              refresh: bool = False) -> dict[str, str]:
    """Environment handed to the remote payload.

    FLEET_DISK_PATHS is omitted rather than emptied when unset: absent means "use the
    built-in mount guess", which is not the same as an empty list of paths.
    """
    env = {"FLEET_MODE": mode}
    if disk_paths:
        # "|", which no path holds: a space split "/mnt/My Data" into two that did not exist.
        env["FLEET_DISK_PATHS"] = "|".join(disk_paths)
    # `fleet top` re-reads every machine every few seconds: it takes what the machine has
    # cached and skips anything that waits on the network. `-r` rebuilds the caches.
    if light:
        env["FLEET_LIGHT"] = "1"
    if refresh:
        env["FLEET_REFRESH"] = "1"
    return env


def run_probe_local(*, mode: str = "full", disk_paths: list[str] | None = None,
                    timeout: float = 20.0, light: bool = False,
                    refresh: bool = False) -> ProbeResult:
    """Probe the machine we are running on, without SSH.

    Requiring sshd, a key in authorized_keys and a working network path in order to
    inspect the machine fleet is already running on is a lot of moving parts for no
    extra information -- and it is the only reason inbound SSH ever had to be enabled
    on a laptop. Same payload, same parser, same ProbeResult as every other path.
    """
    started = time.monotonic()
    env = {**os.environ, **probe_env(mode, disk_paths, light=light, refresh=refresh)}
    # The same choice the remote path makes, for the same reason. Without it a center on
    # Windows probed itself with payload.sh through whatever `sh` Git happened to
    # install, found no /etc/machine-id, and fell back to the `net:localhost:22` id that
    # means "never probed" -- so the machine the access list pins as the center had the
    # one identity that is not stable.
    windows = local_platform() == WINDOWS
    argv = local_shell_argv()
    payload = (PAYLOAD_PS1 if windows else PAYLOAD).read_text(encoding="utf-8")
    try:
        proc = subprocess.run(argv, input=payload.encode(), env=env,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ProbeResult(status=Status.TIMEOUT, error_class="timeout",
                           error_detail=f"local probe exceeded {timeout:.0f}s",
                           endpoint_used="local",
                           latency_ms=int((time.monotonic() - started) * 1000))
    except OSError as exc:
        return ProbeResult(status=Status.PROBE_ERROR, error_class="probe_error",
                           error_detail=str(exc)[:200], endpoint_used="local")
    elapsed = int((time.monotonic() - started) * 1000)
    out = proc.stdout.decode(errors="replace")
    try:
        snap = parse_payload(out)
    except MissingSentinel as exc:
        return ProbeResult(status=Status.PROBE_ERROR, error_class="probe_error",
                           error_detail=str(exc), endpoint_used="local",
                           latency_ms=elapsed,
                           stderr_tail=scrub_stderr(
                               proc.stderr.decode(errors="replace"))[-400:])
    return ProbeResult(status=Status.OK, snapshot=snap, endpoint_used="local",
                       latency_ms=elapsed)


def _posix_remote(env: dict | None) -> str:
    """How to hand the probe script to `sh` on the far side. One form, every platform.

    Two things have to be true, and `sh -s` gives neither.

    The script must not arrive on stdin. ssh.exe will not take a pipe for stdin, so from
    a Windows center it has to come from a file -- and a seekable stdin changes how
    `sh -s` reads it: the identical bytes ran to completion as a file and stalled part
    way on a seekable stdin.

    The script's output must not go straight down the ssh channel. Something the probe
    starts outlives it holding whatever stdout it was handed, which keeps the session
    open after the script has finished -- the probe timed out having already printed
    most of its answer. Give the children a file and only `cat` writes to the channel,
    and `cat` exits.

    Both were found on a Windows center, and for a while this was a Windows-only form.
    It is not any more, because the reason to branch never held up: measured against
    every machine in a real fleet -- including a Synology NAS, the most constrained
    target there is -- this returns the same bytes as `sh -s` and returns them faster.
    A second path that is only exercised on the one platform nobody runs the tests on is
    how the CRLF and pipe bugs survived as long as they did.

    The cost is one writable temp file, where `sh -s` needed nothing. That is a real
    trade and the reason it says so out loud when it cannot have one: the alternative is
    an empty answer that looks like a machine with nothing to report.
    """
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    # In a folder of its own, made fresh: on a shared host anyone can list /tmp, and a
    # file named after the script's ($f.out) or the shell's pid was one somebody else
    # could make first -- a link there and the probe's output truncated a file of ours,
    # a file of theirs and they read it or rewrote the script before sh ran it. `mkdir`
    # fails on a name that is already there; `mktemp -d` picks one nobody has.
    return (
        'd=$(mktemp -d 2>/dev/null) || { d=/tmp/.fleet-probe.$$; mkdir -m 700 "$d"; } '
        '|| { echo "fleet: cannot write a folder for the probe in /tmp" >&2; exit 127; }; '
        'f="$d/probe"; '
        'cat > "$f" || { echo "fleet: cannot write $f" >&2; rm -rf "$d"; exit 127; }; '
        # And a limit on the far side. The probe is detached from the channel by design,
        # so when the local timeout drops the connection nothing tells it to stop: a `df`
        # stuck on a dead NFS mount, or a wedged `nvidia-smi`, stayed behind -- one more
        # on every refresh, on exactly the shared clusters most likely to have them.
        # `timeout` where one takes `-s KILL N` (GNU and busybox both do); else as before.
        'T=; timeout -s KILL 5 true >/dev/null 2>&1 && T="timeout -s KILL 90"; '
        f'{prefix} $T sh "$f" > "$f.out" 2> "$f.err"; rc=$?; '
        'cat "$f.out"; cat "$f.err" >&2; '
        'rm -f "$f" "$f.out" "$f.err"; rm -rf "$d"; exit $rc'
    ).replace("  ", " ").strip()


def _spawn(argv: list[str], payload: bytes, timeout: float,
           popen_kw: dict) -> tuple[int, bytes, bytes]:
    """Start the probe, feed it the script, and collect what came back.

    Two shapes, because Windows needs one. **ssh.exe hangs when its stdin or its stdout
    is an anonymous pipe** -- for good, with no output and no CPU -- so on Windows the
    script goes in through a real file and the answers come back out of real files.
    Measured on the Windows center after it could not reach a single machine: the same
    command with stdout to a pipe timed out every time, and with stdout to a file
    returned in 0.2s.

    The process group survives either way. It is what `_kill` needs to take down ssh and
    everything it started when a host stops answering mid-probe, and losing it would
    leave those behind on every timeout.
    """
    if not IS_WINDOWS:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **popen_kw)
        try:
            out, err = proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            with suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=2)
            raise
        return proc.returncode, out, err

    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "stdin").write_bytes(payload)
        out_path, err_path = root / "stdout", root / "stderr"
        with (root / "stdin").open("rb") as si, out_path.open("wb") as so, \
                err_path.open("wb") as se:
            proc = subprocess.Popen(argv, stdin=si, stdout=so, stderr=se, **popen_kw)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(proc)
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2)
                raise
        return proc.returncode, out_path.read_bytes(), err_path.read_bytes()


def run_probe(ep: Endpoint, *, mode: str = "full", timeout: float = 20.0,
              connect_timeout: int = 8, multiplex: bool = True,
              disk_paths: list[str] | None = None, light: bool = False,
              refresh: bool = False) -> ProbeResult:
    """Probe one endpoint. Never raises for a remote-side problem.

    A host whose shell is cmd.exe is retried in PowerShell. Detected rather than
    configured: the answer is a property of the remote sshd's DefaultShell, which we
    cannot know before asking and which the user should not have to declare. It costs a
    second connection on Windows hosts only, and only the first time each probe runs.
    """
    res = _run_probe_once(ep, mode=mode, timeout=timeout, connect_timeout=connect_timeout,
                          multiplex=multiplex, disk_paths=disk_paths, light=light,
                          refresh=refresh)
    if res.ok:
        return res
    if multiplex and ep.jump and not res.stderr_tail and res.error_class in (
            Status.CLOSED.value, Status.TIMEOUT.value) and "within" not in res.error_detail:
        # A master connection hands its jump host's ssh no stderr to write to, so every
        # failure through a jump host -- its name, its port, its key, or the machine
        # behind it refusing -- came back as one bare "Connection closed", and `fleet
        # add` said to enable sshd. Asked once more without one, ssh says which it was.
        res = _run_probe_once(ep, mode=mode, timeout=timeout,
                              connect_timeout=connect_timeout, multiplex=False,
                              disk_paths=disk_paths, light=light, refresh=refresh)
        if res.ok:
            return res
    again = {"windows": True} if _looks_like_cmd_exe(res) else \
        {"in_sh": True} if _looks_like_another_shell(res) else None
    if again is None:
        return res
    return _run_probe_once(ep, mode=mode, timeout=timeout, connect_timeout=connect_timeout,
                           multiplex=multiplex, disk_paths=disk_paths, light=light,
                           refresh=refresh, **again)


# What a login shell that is not sh says to the probe's sh code -- ssh hands a command to
# the user's own shell, and on a cluster that is often tcsh, on a laptop often fish.
_NOT_SH = ("illegal variable name",           # csh, tcsh: `$(`
           "variable syntax",                 # csh, tcsh
           "unsupported use of '='",          # fish: `d=...`
           "fish: ")


def _looks_like_another_shell(res: ProbeResult) -> bool:
    """Whether the login shell is one that cannot read sh, so the probe goes again
    inside `sh -c`. Not done the first time: a Windows host that happens to have an
    `sh` on its PATH would be handed a quoted string cmd.exe does not unquote."""
    haystack = f"{res.error_detail} {res.stderr_tail}".lower()
    return any(sig in haystack for sig in _NOT_SH)


def _looks_like_cmd_exe(res: ProbeResult) -> bool:
    """Whether to retry this host in PowerShell.

    Reads stderr even when the probe "succeeded": cmd.exe answers the POSIX remote
    command with a single line on stderr and an exit status of 0, so a Windows host
    looks like a POSIX one that ran and reported nothing. Before this, the retry simply
    never fired and every Windows machine came back `payload produced no '#END'`.
    """
    haystack = f"{res.error_detail} {res.stderr_tail}".lower()
    return (WINDOWS_DETAIL.lower() in haystack
            or any(sig in haystack for sig in _NO_POSIX_SHELL))


def _run_probe_once(ep: Endpoint, *, mode: str = "full", timeout: float = 20.0,
                    connect_timeout: int = 8, multiplex: bool = True,
                    disk_paths: list[str] | None = None, light: bool = False,
                    refresh: bool = False, windows: bool = False,
                    in_sh: bool = False) -> ProbeResult:
    env = probe_env(mode, disk_paths, light=light, refresh=refresh)
    if windows:
        # PowerShell reads the script from stdin with a trailing `-`, the same shape as
        # `sh -s`. The environment cannot ride the command line the same way: build_argv
        # prefixes `KEY=value ` in POSIX style, and cmd.exe reads that as the name of a
        # program to run -- producing the very "not recognized" error that sent us here,
        # so the retry failed exactly like the attempt it was retrying. Set it inside the
        # script instead, where PowerShell understands it.
        # Single-quoted, so the one character to escape is the quote itself -- doubled,
        # PowerShell's escape. Unescaped, a disk path such as C:\Users\O'Brien ended the
        # string and ran the rest as code, on a Windows machine, under the center's key.
        prelude = "".join(f"$env:{k}='{str(v).replace(chr(39), chr(39) * 2)}'\n"
                          for k, v in env.items())
        payload = prelude + PAYLOAD_PS1.read_text(encoding="utf-8")
        argv = build_argv(ep, connect_timeout=connect_timeout, multiplex=multiplex,
                          remote="powershell -NoProfile -Command -", env=None)
    else:
        payload = PAYLOAD.read_text(encoding="utf-8")
        remote = _posix_remote(env)
        if in_sh:
            # One line in single quotes reads the same to csh, fish and sh: no `$`
            # or `{` inside them means anything to any of the three.
            remote = f"sh -c {shlex.quote(remote)}"
        argv = build_argv(ep, connect_timeout=connect_timeout, multiplex=multiplex,
                          remote=remote, env=None)
    started = time.monotonic()
    # Bytes, not text. `text=True` wraps stdin in a TextIOWrapper with newline=None,
    # which rewrites every \n to \r\n on Windows -- so a POSIX center sent payload.sh
    # verbatim and a Windows one sent it CRLF, and the far-side `sh` answered
    # `Syntax error: "|" unexpected`. There is no `newline=` on Popen to ask for
    # otherwise, so the script is encoded here and the output decoded back.
    popen_kw: dict = {}
    if IS_WINDOWS:
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kw["start_new_session"] = True

    try:
        rc, raw_out, raw_err = _spawn(argv, payload.encode(), timeout, popen_kw)
        stdout = raw_out.decode(errors="replace")
        stderr = raw_err.decode(errors="replace")
    except subprocess.TimeoutExpired:
        elapsed = int((time.monotonic() - started) * 1000)
        return ProbeResult(status=Status.TIMEOUT, error_class="timeout",
                           error_detail=f"no response within {timeout:.0f}s",
                           endpoint_used=ep.name, latency_ms=elapsed)

    elapsed = int((time.monotonic() - started) * 1000)
    # stdout and stderr are kept separate on purpose: OpenSSH 10.x writes post-quantum
    # warnings to stderr, and merging them would corrupt the line protocol.
    if rc != 0 and not stdout.strip():
        status, detail = classify_stderr(stderr)
        detail = through_jump(ep, stderr, detail)
        return ProbeResult(status=status, error_class=status.value, error_detail=detail,
                           endpoint_used=ep.name, latency_ms=elapsed,
                           stderr_tail=scrub_stderr(stderr)[-400:])
    try:
        snap = parse_payload(stdout)
    except MissingSentinel as exc:
        return ProbeResult(status=Status.PROBE_ERROR, error_class="probe_error",
                           error_detail=str(exc), endpoint_used=ep.name,
                           latency_ms=elapsed, stderr_tail=scrub_stderr(stderr)[-400:])
    return ProbeResult(status=Status.OK, snapshot=snap, endpoint_used=ep.name,
                       latency_ms=elapsed)


def probe_endpoints(endpoints: list[Endpoint], **kw) -> ProbeResult:
    """Try endpoints in preference order; return the first success.

    If all fail, return the most informative failure: auth_failed beats timeout, because
    'the box is up but your key is wrong' is actionable where 'no answer' is not.
    """
    if not endpoints:
        return ProbeResult(status=Status.UNREACHABLE, error_class="no_endpoint",
                           error_detail="device has no endpoints")
    ranked = sorted(endpoints, key=lambda e: e.preference)
    failures: list[ProbeResult] = []
    for ep in ranked:
        res = run_probe(ep, **kw)
        if res.ok:
            return res
        failures.append(res)
    priority = {Status.AUTH_FAILED: 0, Status.HOST_KEY_MISMATCH: 1, Status.PROBE_ERROR: 2,
                Status.CLOSED: 3, Status.REFUSED: 4, Status.TIMEOUT: 5, Status.UNREACHABLE: 6}
    return min(failures, key=lambda r: priority.get(r.status, 9))


def probe_many(jobs: dict[str, list[Endpoint]], *, max_workers: int = 8,
               **kw) -> dict[str, ProbeResult]:
    """Probe many devices concurrently. One dead host cannot delay or fail the others."""
    results: dict[str, ProbeResult] = {}
    if not jobs:
        return results

    def one(eps: list[Endpoint]) -> ProbeResult:
        try:
            return probe_endpoints(eps, **kw)
        except Exception as exc:          # a bug here must not take down the whole sweep
            return ProbeResult(status=Status.PROBE_ERROR, error_class="internal",
                               error_detail=f"{type(exc).__name__}: {exc}"[:200])

    if len(jobs) == 1:
        # In the caller's thread. A pool's worker is joined when Python exits, so `q` in
        # `fleet top` -- whose machines are each asked on a thread of their own -- waited
        # out the probe of a host that never answers.
        ((name, eps),) = jobs.items()
        return {name: one(eps)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        futures = {pool.submit(one, eps): name for name, eps in jobs.items()}
        for fut in concurrent.futures.as_completed(futures):
            results[futures[fut]] = fut.result()
    return results
