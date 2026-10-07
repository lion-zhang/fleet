"""`fleet top` on a Windows console: no termios, and select() takes only sockets.

It died there on "No module named 'termios'" the moment it had a real terminal -- the
e2e run never saw it, because without a terminal top draws one frame and stops.
"""

from __future__ import annotations

import builtins
import sys
import types

from fleet import cli


class _Console:
    def isatty(self):
        return True

    def fileno(self):
        return 0


def _windows(monkeypatch, keys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _Console())
    fake = types.SimpleNamespace(kbhit=lambda: bool(keys), getwch=lambda: keys.pop(0))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    real_import = builtins.__import__

    def no_termios(name, *a, **k):
        if name in ("termios", "tty"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_termios)


def test_raw_mode_needs_no_termios_on_windows(monkeypatch):
    _windows(monkeypatch, [])
    with cli._raw_stdin():
        pass


def test_a_key_is_read_from_the_console(monkeypatch):
    _windows(monkeypatch, ["q"])
    assert cli._key_pressed(1.0) == "q"


def test_no_key_waits_out_the_interval(monkeypatch):
    _windows(monkeypatch, [])
    assert cli._key_pressed(0.1) is None
