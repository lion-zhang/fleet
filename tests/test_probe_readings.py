"""What payload.sh reads off a machine when the machine is not the tidy case.

Each test runs the real payload with stand-ins for one program -- ps, df, nvidia-smi,
ss, lsof -- printing what that program prints on a machine where fleet read it wrong:
a German locale, a broken driver, a disk with a space in its name, a server listening
on IPv6 loopback, a web app answering on a model server's port.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fleet.probe.parse import parse_payload

PAYLOAD = Path(__file__).resolve().parent.parent / "src" / "fleet" / "probe" / "payload.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                                reason="runs payload.sh")
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's ps columns")


def _stub(bin_: Path, name: str, script: str) -> None:
    (bin_ / name).write_text("#!/bin/sh\n" + script)
    (bin_ / name).chmod(0o755)


def _probe(tmp_path: Path, **env) -> str:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bin_ = tmp_path / "bin"
    e = {"PATH": f"{bin_}:{os.environ['PATH']}", "HOME": str(home), **env}
    p = subprocess.run(["sh", str(PAYLOAD)], env=e, capture_output=True, text=True,
                       timeout=60)
    assert "#END" in p.stdout, p.stderr
    return p.stdout


@pytest.fixture
def bin_(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


@linux
def test_cpu_use_is_read_in_the_parser_s_numbers_whatever_the_locale(tmp_path, bin_):
    """ssh forwards LANG and LC_*; under de_DE ps prints "12,5", read as 12."""
    _stub(bin_, "ps", """case "$*" in *pcpu*)
  if [ -z "$LC_ALL" ] && [ "$LC_NUMERIC" = C ]; then c=12.5; else c=12,5; fi
  echo "  4242     1 me $c 1000 100 trainer" ;; esac
""")
    snap = parse_payload(_probe(tmp_path, LC_ALL="de_DE.UTF-8"))
    [p] = [p for p in snap.processes if p.pid == 4242]
    assert p.cpu_pct == 12.5


@linux
def test_a_program_s_name_with_a_space_is_kept_whole(tmp_path, bin_):
    _stub(bin_, "ps", """case "$*" in *pcpu*) echo "  4242     1 me 50.0 1000 100 Web Content" ;; esac
""")
    [p] = [p for p in parse_payload(_probe(tmp_path)).processes if p.pid == 4242]
    assert p.comm == "Web Content"


def test_a_broken_gpu_driver_is_reported_as_broken(tmp_path, bin_):
    """It says so on stdout, which was taken for the driver version."""
    _stub(bin_, "nvidia-smi", 'echo "Failed to initialize NVML: Driver/library version '
                              'mismatch"\nexit 18\n')
    snap = parse_payload(_probe(tmp_path))
    assert snap.gpu_present == "err" and "version mismatch" in snap.gpu_error
    assert snap.gpus == [] and snap.gpu_driver == ""


def test_a_disk_with_a_space_in_its_name_is_reported_by_its_name(tmp_path, bin_):
    _stub(bin_, "df", """echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/disk4s1 976000000 900000000 76000000 93% /Volumes/My Passport"
""")
    disks = [d.mount for d in parse_payload(_probe(tmp_path)).disks]
    assert "/Volumes/My Passport" in disks


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/mounts")
def test_a_disk_you_cannot_write_to_can_still_fill_up(tmp_path):
    """-w is false for a user on any disk owned by root, so a full /home never alerted.
    Whether it can fill is the mount's own read-only flag."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        out = _probe(tmp_path, FLEET_DISK_PATHS=str(locked))
    finally:
        locked.chmod(0o755)
    [disk] = [d for d in parse_payload(out).disks if d.mount == str(locked)]
    assert disk.writable


# ------------------------------------------------------------------ model servers
class _Web(http.server.BaseHTTPRequestHandler):
    """Answers every path with 200, as a web app does."""
    body = b"<!doctype html><title>dashboard</title>"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802 - http.server's name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.body)


def _serve(handler, family=socket.AF_INET, host="127.0.0.1"):
    cls = type("S", (http.server.ThreadingHTTPServer,), {"address_family": family})
    srv = cls((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _listening(bin_: Path, addr: str, port: int) -> None:
    """ss on Linux, lsof on macOS, both saying a vllm process listens there."""
    _stub(bin_, "ss", f"""printf '%s\\n' 'LISTEN 0 128 {addr}:{port} *:* users:(("vllm",pid=1,fd=3))'\n""")
    _stub(bin_, "lsof", "echo 'COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME'\n"
                        f"echo 'vllm 1 me 3u IPv6 0x1 0t0 TCP {addr}:{port} (LISTEN)'\n")


need_http = pytest.mark.skipif(not (shutil.which("curl") or shutil.which("wget")),
                               reason="curl or wget")


@need_http
@pytest.mark.parametrize("body", [b"<!doctype html><title>dashboard</title>",
                                  json.dumps({"id": "user-7", "name": "me"}).encode()])
def test_a_web_app_on_a_model_server_s_port_is_not_a_model_server(tmp_path, bin_, body):
    """A 200 with a page, or with any JSON carrying an "id", was a healthy model server."""
    srv = _serve(type("W", (_Web,), {"body": body}))
    try:
        port = srv.server_address[1]
        _listening(bin_, "127.0.0.1", port)
        [svc] = [s for s in parse_payload(_probe(tmp_path)).services if s.port == port]
        assert svc.status != "ok" and svc.model_ids == []
    finally:
        srv.shutdown()


@need_http
def test_a_server_on_ipv6_loopback_only_is_asked_there(tmp_path, bin_):
    """It was asked at 127.0.0.1, where nothing listens, and showed as down."""
    class Models(_Web):
        body = json.dumps({"object": "list", "data": [{"id": "qwen3"}]}).encode()

    try:
        srv = _serve(Models, socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("no IPv6 loopback here")
    try:
        port = srv.server_address[1]
        _listening(bin_, "[::1]", port)
        [svc] = [s for s in parse_payload(_probe(tmp_path)).services if s.port == port]
        assert svc.status == "ok" and svc.model_ids == ["qwen3"]
        assert svc.addr == "[::1]"
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ folder sizes
def test_a_folder_with_a_space_in_its_name_is_measured(tmp_path):
    """The list of folders to measure was split on spaces, so "My Models" was two
    folders, neither of which existed."""
    import time

    home = tmp_path / "home"
    (home / ".config" / "fleet").mkdir(parents=True)
    (home / ".config" / "fleet" / "machine.yaml").write_text(
        "paths:\n  models: ~/My Models\n")
    (home / "My Models").mkdir()
    (home / "My Models" / "w.bin").write_bytes(b"x" * 300_000)
    (tmp_path / "bin").mkdir()
    _probe(tmp_path)
    sizes = home / ".cache" / "fleet" / "sizes"
    for _ in range(100):
        if sizes.exists():
            break
        time.sleep(0.05)
    [store] = parse_payload(_probe(tmp_path)).storage
    assert store.size_kb and store.size_kb >= 290


# ------------------------------------------------------------------ redaction
@pytest.mark.parametrize("line, secret", [
    ("python run.py --key=abc123", "abc123"),
    ("python run.py --key abc123", "abc123"),
    ("psql postgres://me:hunter2@db/prod", "hunter2"),
    ('python serve.py --token "two words"', "words"),
    ("HF_TOKEN='a b' python serve.py", "b'"),
])
def test_a_secret_in_a_job_s_command_line_stays_on_the_machine(line, secret):
    from fleet.probe.parse import redact

    assert secret not in redact(line)


def test_redaction_leaves_ordinary_flags_alone():
    from fleet.probe.parse import redact

    line = "python train.py --keyframes 3 --monkey 2 --lr 3e-4 http://host:8000/v1"
    assert redact(line) == line
