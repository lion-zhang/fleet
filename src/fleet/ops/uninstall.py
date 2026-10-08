"""Taking fleet off this machine: out of its fleet, out of its agents, and its files gone.

Each step already existed on its own -- `center --leave`, `setup --uninstall`, `service
remove` -- and removing fleet meant knowing which of them to run, in which order, on
which kind of machine. Run in the wrong order it strands keys: a program deleted before
the machine left its fleet leaves the fleet's keys in authorized_keys with nothing on
the machine that knows they are there.

What it never does is remove the program itself. A running program cannot reliably
delete its own installation (on Windows not at all), and how it was installed -- uv,
pipx, a checkout -- decides the command. So the last step is printed, not run.
"""

from __future__ import annotations

import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..state import access as acl
from .errors import FleetError


@dataclass
class Outcome:
    left: str = ""                     # "member", "empty center", or "" (in no fleet)
    fleet_id: str = ""
    key_blocks_removed: int = 0
    agents: list[str] = field(default_factory=list)
    service: str = ""
    files_removed: list[str] = field(default_factory=list)
    files_kept: list[str] = field(default_factory=list)
    remove_program: list[str] = field(default_factory=list)
    center: str = ""                   # the center that still lists this machine
    name: str = ""                     # what that center calls it


def refusal() -> str:
    """Why this machine cannot uninstall yet, or "".

    A center with other machines in it holds the only key that can remove its own keys
    from them. Uninstalled, those keys stay on every machine with nothing left that can
    take them off -- so the role is handed on, or the fleet dissolved, first.
    """
    try:
        acc = acl.load()
    except acl.AccessError:
        return ""
    if not acl.is_center(acc):
        return ""
    others = set(acc.keys) - {acc.center}
    if others or acc.allow:
        return (f"this machine is the center of fleet {acc.fleet_id}, with "
                f"{len(others)} other machine(s) in it")
    return ""


def uninstall(*, purge: bool) -> Outcome:
    """Leave the fleet, unteach the agents, remove the service; with `purge`, the files.

    Raises FleetError when `refusal()` has a reason. The caller asks first.
    """
    from .. import service
    from ..agents import installed_mcp_clients, installed_targets, uninstall as unteach
    from ..agents import uninstall_mcp
    from . import lifecycle, member

    if why := refusal():
        raise FleetError(why, code=2)
    out = Outcome()

    where = lifecycle.membership()
    if where == "center":
        acc = acl.load()
        out.fleet_id = acc.fleet_id
        lifecycle.retire_empty_center(acc)
        out.left = "empty center"
    elif where == "member":
        from ..state import inventory as inv
        from . import identity

        out.center = member.center_name() or ""
        me = next((d for d in inv.live(inv.load()) if d.id == identity.local_device_id()), None)
        out.name = me.name if me else ""
        out.fleet_id, out.key_blocks_removed = member.leave()
        out.left = "member"

    home = Path.home()
    targets = installed_targets(home)
    clients = installed_mcp_clients(home)
    changes = unteach(home, targets) + uninstall_mcp(home, clients)
    out.agents = [str(c.path) for c in changes if c.action == "removed"]

    with suppress(Exception):
        if service.status(0).startswith((service.INSTALLED, service.RUNNING)):  # one there
            out.service = service.remove()

    for path in _fleet_files():
        if not path.exists():
            continue
        if purge:
            with suppress(OSError):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                out.files_removed.append(str(path))
        elif not path.name.startswith("."):
            out.files_kept.append(str(path))
    if purge:
        for d in {config.CONFIG_DIR, config.STATE_DIR}:
            with suppress(OSError):
                d.rmdir()                      # only if nothing else is in it
    out.remove_program = how_to_remove_program()
    return out


def _fleet_files() -> list[Path]:
    """Every file fleet keeps, by name.

    Never by globbing its directories: they may be the same directory, or one someone
    pointed FLEET_CONFIG_DIR at that holds other things too. Each file's write-queue
    folder (`.NAME.queue`) goes with it.
    """
    from ..state import clock, invites, store
    from ..state import inventory as inv
    from . import identity

    cfg, st = config.CONFIG_DIR, config.STATE_DIR
    named = [
        inv.INVENTORY_PATH, config.FLEET_KEY, config.FLEET_KEY.with_suffix(".pub"),
        acl.ACCESS_PATH, acl.ACCESS_PATH.with_name("access.retired.yaml"),
        config.CONFIG_PATH, identity.device_id_file(),
        acl.LEDGER_PATH, acl.CACHE_PATH, acl.OUTBOX_PATH, acl.CHAIN_PATH,
        acl.INBOX_PATH, acl.HANDING_PATH, invites.INVITES_PATH, clock.OFFSET_PATH,
        store.DB_PATH, store.DB_PATH.with_name(store.DB_PATH.name + "-wal"),
        store.DB_PATH.with_name(store.DB_PATH.name + "-shm"),
        st / "center-service.log", st / "center-service.log.1",
        st / "update.log", st / "update.ps1", st / "update-run.ps1", st / "update-run.log",
    ]
    queues = [p.with_name(f".{p.name}.queue") for p in named] + [st / ".refresh.queue"]
    seen, out = set(), []
    for p in named + queues:
        if p not in seen and p.parent in (cfg, st):
            seen.add(p)
            out.append(p)
    return out


def how_to_remove_program() -> list[str]:
    """The commands that remove the program itself, for how it was installed here."""
    from ..install import install_source

    here = Path(__file__).resolve()
    if any(p.lower() == "pipx" for p in here.parts):
        return ["pipx uninstall agents-fleet"]
    uv_tool = any((parent / "uv-receipt.toml").is_file() for parent in here.parents)
    if not uv_tool:
        # `uv run` from a checkout, pip into some environment: not ours to name.
        return [f"remove fleet the way it was installed (it runs from {here.parents[1]})"]
    lines = ["uv tool uninstall agents-fleet"]
    checkout = Path.home() / ".local" / "share" / "fleet"
    src = install_source()
    if src is not None and src.resolve() == checkout.resolve():
        lines.append(f"then delete {checkout} -- the checkout `fleet install` made")
    return lines
