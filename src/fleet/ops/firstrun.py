"""The first run: a machine in no fleet becomes the center, and its agents learn fleet.

Nobody should have to know `fleet center --init` and `fleet setup` exist before fleet is
useful. So the installer runs both, and for every other way in -- `uv tool install`,
pipx, a plugin, an MCP client launching `fleet mcp` -- the first real command does the
same, once, and says so in a line.

The one thing that must never happen is a machine that was meant to be a *member*
making itself a center. That is how a second, competing fleet is born. So it is opted
into by name rather than out of: only the commands a person or an agent uses to look at
or grow a fleet trigger it, never the ones by which machines are made members (`sync`,
`join`, `center --accept`/`--receive`, and everything `fleet install` runs on a machine
it is installing, which also sets FLEET_NO_AUTO_CENTER).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..state import access as acl
from ..state import inventory as inv
from ..state import store

OPT_OUT = "FLEET_NO_AUTO_CENTER"


def init_center():
    """Start a fleet with this machine as its center. Returns (access, device).

    Raises AccessError when this machine already holds a fleet's access list.
    """
    # Through the modules, not imported names, so there is one place to substitute them.
    from .. import onboard
    from ..ssh import keys

    _, pub = keys.ensure_keypair()
    devices = inv.load()
    dev, res = onboard.onboard_self()
    # Re-running --init must not rename this machine. Passing every existing name as
    # taken counted its *own* record among them, so a second --init came back as
    # "<name>-2" and pinned that into the access list while the inventory kept the
    # first -- the exact name split seeding both from one object exists to prevent.
    if existing := inv.find_exact(devices, dev.id):
        dev.name = existing.name
    else:
        taken, base, n = inv.handles(devices), dev.name, 2
        while dev.name in taken:
            dev.name, n = f"{base}-{n}", n + 1
    acc = acl.bootstrap(dev.name, pub, dev.id)
    # Reflect the role in the inventory too. `is_center()` remains the authority --
    # this field rides the merge and cannot be trusted for a decision -- but it is
    # what `ls` and `top` draw the diamond from.
    dev.role = "center"
    # Seed the inventory from the same object the access list was pinned from, so the
    # two never derive a name each.
    devices, _ = inv.upsert(devices, dev)
    inv.save(devices)
    if res.snapshot is not None:
        conn = store.connect()
        try:
            store.record(conn, dev.id, res)
        finally:
            conn.close()
    return acc, dev


def teach_agents() -> list[str]:
    """`fleet setup` for every agent installed here. Returns the agents taught."""
    from ..agents import (config_command, detect_mcp_clients, detect_targets,
                          fleet_command, install, install_mcp)

    root = Path.home()
    changes = (install(root, detect_targets(root), fleet_command())
               + install_mcp(root, detect_mcp_clients(root), config_command()))
    return sorted({c.target for c in changes if c.action in ("created", "updated")})


def maybe(command: str, say) -> bool:
    """Run the first run if this machine is in no fleet yet. Returns whether it ran.

    Called by name from the commands that mean "I am using fleet here" -- `ls`, `show`,
    `top`, `add`, `invite`, `access`, bare `center`, `setup` when it is setting up, and
    `mcp` -- each of which knows its own flags. Never from the ones that make a machine
    a member. `say` prints one line; the MCP server passes a stderr printer, because its
    stdout is the protocol.
    """
    from .lifecycle import membership

    if os.environ.get(OPT_OUT) or membership() != "":
        return False
    try:
        acc, dev = init_center()
    except acl.AccessError:
        return False
    taught = [] if command == "setup" else teach_agents()
    say(f"started fleet {acc.fleet_id} with {dev.name} as its center"
        + (f"; taught {', '.join(taught)} to use it" if taught else "")
        + f"  (to join another fleet instead: fleet join CODE)")
    return True
