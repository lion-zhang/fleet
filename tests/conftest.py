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
    from fleet import cli

    monkeypatch.setattr(cli, "_post", lambda *a, **k: None, raising=False)
    return root
