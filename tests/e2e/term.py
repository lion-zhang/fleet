"""A real terminal for a command, driven as a person at a keyboard drives it.

POSIX: a pseudo-terminal that is the command's controlling tty, so Ctrl+C is a signal
from the line discipline and full-screen programs see a terminal. Windows: a ConPTY
pseudo-console through pywinpty, which is what Windows Terminal gives a program -- the
only way to test console input there, since a CI runner has no console of its own.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time

WINDOWS = os.name == "nt"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\x1b[=>78]")


def reporting_exit(argv: list[str]) -> list[str]:
    """On Windows, run argv inside PowerShell, which prints its exit code as a marker.

    pywinpty's exitstatus is not reliable (it read `cmd /c exit 5` as 1), so the code is
    read from the terminal instead. Elsewhere argv is returned as it is.
    """
    if not WINDOWS:
        return argv
    call = " ".join("'" + a.replace("'", "''") + "'" for a in argv)
    return ["powershell", "-NoProfile", "-Command",
            f"& {call}; Write-Output ('FLEET-EXIT=' + $LASTEXITCODE)"]


def clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", "")


class Term:
    def __init__(self, argv: list[str], *, env: dict | None = None, cols: int = 140,
                 rows: int = 40):
        self.out: queue.Queue[str] = queue.Queue()
        self.text = ""
        if WINDOWS:
            from winpty import PtyProcess

            self.proc = PtyProcess.spawn(subprocess.list2cmdline(argv), env=env,
                                         dimensions=(rows, cols))
            self._reader = threading.Thread(target=self._read_winpty, daemon=True)
        else:
            import fcntl
            import pty
            import struct
            import termios

            self.master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

            def own_the_tty():                 # setsid ran first (start_new_session)
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                         env=env, start_new_session=True,
                                         preexec_fn=own_the_tty)
            os.close(slave)
            self._reader = threading.Thread(target=self._read_pty, daemon=True)
        self._reader.start()

    def _read_pty(self) -> None:
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            self.out.put(data.decode("utf-8", "replace"))

    def _read_winpty(self) -> None:
        while True:
            try:
                data = self.proc.read(4096)
            except EOFError:
                return
            except Exception:
                if not self.proc.isalive():
                    return
                time.sleep(0.05)
                continue
            if data:
                self.out.put(data)

    def _drain(self, wait: float) -> None:
        try:
            self.text += self.out.get(timeout=wait)
            while True:
                self.text += self.out.get_nowait()
        except queue.Empty:
            pass

    def send(self, keys: str, *, per_key: float = 0.0) -> None:
        """Type `keys`. per_key > 0 types them one at a time, as a person does."""
        chunks = list(keys) if per_key else [keys]
        for c in chunks:
            if WINDOWS:
                self.proc.write(c)
            else:
                os.write(self.master, c.encode())
            if per_key:
                time.sleep(per_key)

    def expect(self, pattern: str, timeout: float = 20.0, *, since: int = 0) -> bool:
        deadline = time.monotonic() + timeout
        rx = re.compile(pattern, re.M)
        while time.monotonic() < deadline:
            if rx.search(clean(self.text[since:])):
                return True
            self._drain(0.2)
        return bool(rx.search(clean(self.text[since:])))

    def mark(self) -> int:
        self._drain(0.1)
        return len(self.text)

    def alive(self) -> bool:
        return self.proc.isalive() if WINDOWS else self.proc.poll() is None

    def wait(self, timeout: float = 20.0) -> int | None:
        """The exit code, or None if it is still running after `timeout`.

        On Windows it is the one `reporting_exit` printed, read off the terminal."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                self._drain(0.3)
                if WINDOWS:
                    found = re.findall(r"FLEET-EXIT=(-?\d+)", clean(self.text))
                    return int(found[-1]) if found else None
                return self.proc.returncode
            self._drain(0.2)
        return None

    def close(self) -> None:
        if self.alive():
            try:
                if WINDOWS:
                    self.proc.terminate(force=True)
                else:
                    self.proc.kill()
            except Exception:
                pass
        if not WINDOWS:
            try:
                os.close(self.master)
            except OSError:
                pass

    def tail(self, n: int = 1500) -> str:
        return clean(self.text)[-n:]
