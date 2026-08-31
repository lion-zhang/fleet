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
import subprocess
import time
from pathlib import Path

from ..models import ProbeResult, Snapshot, Status
from ..sshcmd import IS_WINDOWS, Endpoint, build_argv
from .parse import MissingSentinel, parse_payload

PAYLOAD = Path(__file__).with_name("payload.sh")

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
    ("Operation timed out", Status.TIMEOUT, "network timeout"),
    ("Connection timed out", Status.TIMEOUT, "network timeout"),
    ("Could not resolve hostname", Status.UNREACHABLE, "DNS lookup failed"),
    ("Name or service not known", Status.UNREACHABLE, "DNS lookup failed"),
    ("No route to host", Status.UNREACHABLE, "no route"),
    ("Network is unreachable", Status.UNREACHABLE, "network unreachable"),
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
)


def scrub_stderr(stderr: str) -> str:
    keep = [ln for ln in stderr.splitlines()
            if ln.strip() and not any(b in ln for b in _BENIGN_STDERR)]
    return "\n".join(keep)


def classify_stderr(stderr: str) -> tuple[Status, str]:
    cleaned = scrub_stderr(stderr)
    for needle, status, detail in _ERROR_SIGNATURES:
        if needle in cleaned:
            return status, detail
    last = (cleaned.strip().splitlines() or [""])[-1][:200]
    if not last:
        # ssh failed but said nothing at all -- almost always the peer hanging up during
        # banner exchange, which is what a recycled rental port looks like.
        return Status.CLOSED, "peer closed the connection without a diagnostic"
    return Status.PROBE_ERROR, last


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


def run_probe(ep: Endpoint, *, mode: str = "full", timeout: float = 20.0,
              connect_timeout: int = 8, multiplex: bool = True) -> ProbeResult:
    """Probe one endpoint. Never raises for a remote-side problem."""
    payload = PAYLOAD.read_text()
    argv = build_argv(ep, connect_timeout=connect_timeout, multiplex=multiplex,
                      remote="sh -s", env={"FLEET_MODE": mode})
    started = time.monotonic()
    popen_kw: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                      "stderr": subprocess.PIPE, "text": True}
    if IS_WINDOWS:
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kw["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kw)
    try:
        stdout, stderr = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        elapsed = int((time.monotonic() - started) * 1000)
        return ProbeResult(status=Status.TIMEOUT, error_class="timeout",
                           error_detail=f"no response within {timeout:.0f}s",
                           endpoint_used=ep.name, latency_ms=elapsed)

    elapsed = int((time.monotonic() - started) * 1000)
    # stdout and stderr are kept separate on purpose: OpenSSH 10.x writes post-quantum
    # warnings to stderr, and merging them would corrupt the line protocol.
    if proc.returncode != 0 and not stdout.strip():
        status, detail = classify_stderr(stderr)
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        futures = {pool.submit(probe_endpoints, eps, **kw): name
                   for name, eps in jobs.items()}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:      # a bug here must not take down the whole sweep
                results[name] = ProbeResult(
                    status=Status.PROBE_ERROR, error_class="internal",
                    error_detail=f"{type(exc).__name__}: {exc}"[:200])
    return results
