"""The first run: this machine is recorded, and its agents learn fleet.

Nobody should have to know `fleet add --self` and `fleet setup` exist before fleet is
useful. So the installer runs setup, and for every other way in -- `uv tool install`,
pipx, a plugin, an MCP client launching `fleet mcp` -- the first real command does the
same, once, and says so in a line. There is no fleet to start or join: every machine
running fleet is the same, and looks at the machines its own SSH can reach.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..state import inventory as inv
from ..state import store

OPT_OUT = "FLEET_NO_FIRST_RUN"


def _marker() -> Path:
    from ..config import STATE_DIR

    return STATE_DIR / "first-run-done"


def add_this_machine():
    """Record the machine fleet runs on, if it is not recorded yet. Returns it or None."""
    from .. import onboard
    from . import identity

    me = identity.local_device_id()
    devices = inv.load()
    if me and inv.find_exact(devices, me):
        return None
    dev, res = onboard.onboard_self(taken_names=inv.handles(devices))
    inv.update(lambda current: inv.upsert(current, dev))
    if res.snapshot is not None:
        conn = store.connect()
        try:
            store.record(conn, dev.id, res)
        finally:
            conn.close()
    return dev


def teach_agents() -> list[str]:
    """`fleet setup` for every agent installed here. Returns the agents taught."""
    from ..agents import (config_command, detect_mcp_clients, detect_targets,
                          fleet_command, install, install_mcp)

    root = Path.home()
    changes = (install(root, detect_targets(root), fleet_command())
               + install_mcp(root, detect_mcp_clients(root), config_command()))
    return sorted({c.target for c in changes if c.action in ("created", "updated")})


def maybe(command: str, say) -> bool:
    """Run the first run if it has not run here yet. Returns whether it ran.

    `say` prints one line; the MCP server passes a stderr printer, because its stdout is
    the protocol.
    """
    from ..config import ensure_dirs
    from ..state.writes import atomic_write, turn

    if os.environ.get(OPT_OUT) or _marker().exists():
        return False
    ensure_dirs()
    # Two agents' first commands at the same moment: the second waits for the first,
    # then finds it done.
    with turn(_marker()):
        if _marker().exists():
            return False
        dev = add_this_machine()
        taught = [] if command == "setup" else teach_agents()
        atomic_write(_marker(), "1\n", mode=0o644)
    bits = ([f"recorded {dev.name}"] if dev else []) + (
        [f"taught {', '.join(taught)} to use it"] if taught else [])
    if bits:
        say("fleet is ready here: " + "; ".join(bits)
            + "  (add machines with fleet add \"ssh user@host\", or see fleet ls --discover)")
    return True
