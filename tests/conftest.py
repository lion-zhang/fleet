"""Keep the suite out of the real fleet.

Every path fleet writes to is redirected into a temp directory for every test, whether
the test asks or not. Tests that sandbox explicitly still work -- their monkeypatching
runs after this and simply wins.

This exists because the alternative kept happening. A `--serve` test once pinned a
center into the real `~/Library/Application Support/fleet`; a `bootstrap()` call in a
unit test created a real `access.yaml` and made this laptop believe it was a center; and
once commands began refreshing themselves from the center, any test that ran `fleet ls`
started making live HTTP requests to the production fleet -- which was slow, and was
reading a real machine's state into test assertions.

The rule worth keeping: a test that has not said where state lives must not be able to
find any.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _sandbox_fleet_state(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("fleet-state")

    from fleet import access as acl
    from fleet import config, inventory as inv, store

    for name in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, name, root / getattr(acl, name).name, raising=False)
    monkeypatch.setattr(inv, "INVENTORY_PATH", root / "inventory.yaml", raising=False)
    monkeypatch.setattr(store, "DB_PATH", root / "cache.db", raising=False)
    monkeypatch.setattr(config, "FLEET_KEY", root / "id_ed25519", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", root / "config.yaml", raising=False)
    monkeypatch.setattr(config, "INVENTORY_PATH", root / "inventory.yaml", raising=False)

    # Belt and braces for the one that reaches the network: a command that refreshes
    # itself must not dial anything real, even if some path above is missed.
    from fleet.ops import sync

    monkeypatch.setattr(sync, "post", lambda *a, **k: None, raising=False)
    return root


class EscapedToTheNetwork(AssertionError):
    """A test opened a socket to somewhere real."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    """Fail a test that reaches the network, naming what it dialled.

    This exists for the reorganisation, and it earns its place regardless. Tests patch
    dozens of names on `fleet.cli` -- `run_probe`, `onboard`, `install_key` -- because
    that is the module the code reads them from. Move a function into `fleet.ops` and
    those patches still apply to `cli`, still pass, and no longer intercept anything: the
    call goes through to a real machine. That failure is silent, slow, and indistinguish-
    able from a test that is merely thorough.

    Loopback stays open, because the HTTP tests bind a real port on 127.0.0.1 and that is
    the point of them.
    """
    import socket

    if "allow_network" in request.keywords:
        return

    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def _local(address) -> bool:
        host = address[0] if isinstance(address, tuple) else ""
        return host in ("127.0.0.1", "::1", "localhost", "", None)

    def guarded_connect(self, address, *a, **k):
        if not _local(address):
            raise EscapedToTheNetwork(
                f"this test dialled {address!r}. Something it meant to stub is not "
                "stubbed -- most likely a monkeypatch pointing at a module the code no "
                "longer reads the name from.")
        return real_connect(self, address, *a, **k)

    def guarded_create(address, *a, **k):
        if not _local(address):
            raise EscapedToTheNetwork(f"this test dialled {address!r}")
        return real_create(address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create)

    # The case that actually matters here. fleet reaches machines by spawning ssh, not by
    # opening a socket in Python, so a socket guard alone protects the minority path --
    # `run_probe`, `apply_edge`, `run_sync` and `install_key` would all still dial out.
    # Matched on the basename exactly: `ssh-keygen` is local and tests run it for real.
    import subprocess

    real_run, real_popen = subprocess.run, subprocess.Popen

    def _remote_program(argv) -> str:
        if isinstance(argv, (str, bytes)):
            return ""
        head = str(argv[0]) if argv else ""
        return head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    def _refuse(argv):
        name = _remote_program(argv)
        if name in ("ssh", "scp", "sftp", "ssh-copy-id"):
            raise EscapedToTheNetwork(
                f"this test spawned {name!r}: {list(argv)[:4]}. fleet reaches machines by "
                "running ssh, so a stub that no longer applies shows up here rather than "
                "as a failure -- check what this test meant to patch.")

    def guarded_run(argv, *a, **k):
        _refuse(argv)
        return real_run(argv, *a, **k)

    class GuardedPopen(real_popen):
        # A subclass, not a function: libraries treat subprocess.Popen as a type and
        # some subscript or isinstance it, which a plain wrapper breaks.
        def __init__(self, argv, *a, **k):
            _refuse(argv)
            super().__init__(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
