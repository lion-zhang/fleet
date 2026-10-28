"""Taking fleet off this machine: out of its agents, and its files gone.

fleet changes nothing on the machines it looks at except the notes `fleet edit --path`
writes, so there is nothing to undo on them: this is about this machine only.

What it never does is remove the program itself. A running program cannot reliably
delete its own installation (on Windows not at all), and how it was installed -- uv,
pipx, a checkout -- decides the command. So the last step is printed, not run.
"""

from __future__ import annotations

import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .. import config


@dataclass
class Outcome:
    agents: list[str] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)
    files_kept: list[str] = field(default_factory=list)
    remove_program: list[str] = field(default_factory=list)


def uninstall(*, purge: bool) -> Outcome:
    """Unteach the agents; with `purge`, delete fleet's files too."""
    from ..agents import installed_mcp_clients, installed_targets, uninstall as unteach
    from ..agents import uninstall_mcp

    out = Outcome()
    home = Path.home()
    targets = installed_targets(home)
    clients = installed_mcp_clients(home)
    changes = unteach(home, targets) + uninstall_mcp(home, clients)
    out.agents = [str(c.path) for c in changes if c.action == "removed"]

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
        out.files_removed += _drop_probe_cache()
    out.remove_program = how_to_remove_program()
    return out


def _drop_probe_cache() -> list[str]:
    """The cache the probe keeps on every machine it reads -- this one included, since
    fleet reads the machine it runs on. --purge left it behind.

    By name, as everywhere here: `software` and `sizes`, and the lock `du` holds, in the
    folder each machine keeps them in (a home shared by several has one for each), and
    where they were kept before that.
    """
    import os

    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    cache = Path(base) / "fleet"
    removed = []
    try:
        folders = [d for d in cache.iterdir() if d.is_dir() and d.name != "du.lock"]
    except OSError:
        folders = []
    for folder in [*folders, cache]:
        for name in ("software", "sizes"):
            with suppress(OSError):
                (folder / name).unlink()
                removed.append(str(folder / name))
        for d in (folder / "du.lock", folder):
            with suppress(OSError):
                d.rmdir()
    return removed


def _fleet_files() -> list[Path]:
    """Every file fleet keeps, by name.

    Never by globbing its directories: they may be the same directory, or one someone
    pointed FLEET_CONFIG_DIR at that holds other things too. Each file's write-queue
    folder (`.NAME.queue`) goes with it.
    """
    from ..state import store
    from ..state import inventory as inv
    from . import identity

    cfg, st = config.CONFIG_DIR, config.STATE_DIR
    named = [
        inv.INVENTORY_PATH, config.CONFIG_PATH, identity.device_id_file(),
        cfg / "discover-hidden.txt",
        st / "first-run-done",
        store.DB_PATH, store.DB_PATH.with_name(store.DB_PATH.name + "-wal"),
        store.DB_PATH.with_name(store.DB_PATH.name + "-shm"),
        st / "update.log", st / "update.ps1", st / "update-run.ps1", st / "update-run.log",
    ]
    queues = [p.with_name(f".{p.name}.queue") for p in named]
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
