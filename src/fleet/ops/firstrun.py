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
    try:
        ensure_dirs()
    except OSError:
        return False        # nowhere to write it (a sandbox): next time, not this command
    # Two agents' first commands at the same moment: the second waits for the first,
    # then finds it done.
    try:
        with turn(_marker()):
            if _marker().exists():
                return False
            dev = add_this_machine()
            taught, failed = [], ""
            if command != "setup":
                try:
                    taught = teach_agents()
                except Exception as exc:      # noqa: BLE001 -- see below
                    # Teaching writes into other programs' files, and anything wrong
                    # with one of them raised out of here before the marker was
                    # written: the first run then ran, and failed, again on every
                    # command after.
                    failed = f"{type(exc).__name__}: {exc}"
            atomic_write(_marker(), "1\n", mode=0o644)
    except OSError:
        # The folder is there and may not be written -- a sandbox that lets fleet read
        # its state, not keep it. Every command died here, the one that was asked for
        # never running; it is the first run's to do next time, as with no folder.
        return False
    if failed:
        from rich.markup import escape

        say(f"could not teach this machine's agents fleet ({escape(failed)}); "
            "fleet setup tries again", "warn")
    bits = ([f"recorded {dev.name}"] if dev else []) + (
        [f"taught {', '.join(taught)} to use it"] if taught else [])
    if bits:
        say("fleet is ready here: " + "; ".join(bits)
            + "  (add machines with fleet add \"ssh user@host\", or see fleet ls --discover)")
    if left := _left_by_0_5(dev):
        # 0.6 does not read 0.5's list, by design -- but saying nothing sent people to
        # the troubleshooting page with "my machines are gone".
        say(f"fleet 0.5's list (inventory.yaml) has {left} other machine(s), which 0.6 "
            "does not read: add them again -- its addresses are still in that file, and "
            "fleet ls --discover lists your ssh hosts", "warn")
    return True


def _left_by_0_5(me) -> int:
    """How many machines, besides this one, fleet 0.5's inventory.yaml held."""
    from ..config import CONFIG_DIR

    try:
        import yaml

        raw = yaml.safe_load((CONFIG_DIR / "inventory.yaml").read_text(encoding="utf-8"))
    except Exception:                      # noqa: BLE001 -- absent or unreadable: say nothing
        return 0
    devices = raw.get("devices") if isinstance(raw, dict) else None
    if not isinstance(devices, list):
        return 0
    mine = getattr(me, "id", None)
    from . import identity

    mine = mine or identity.local_device_id()
    return sum(1 for d in devices if isinstance(d, dict) and d.get("id") != mine
               and d.get("role") != "center")
