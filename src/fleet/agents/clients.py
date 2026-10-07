"""Registering fleet with the desktop clients that speak MCP rather than read a file.

The other half of `fleet setup`, and it writes JSON someone else owns -- a client's
config file, which may hold servers fleet knows nothing about. So it edits one key under
`mcpServers` and leaves the document otherwise exactly as found, including the keys and
the formatting.

Same rule as `docs.py`, arrived at from the other direction: never touch a byte we did
not write.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..state.writes import replace_file
from .registry import MCP_CLIENTS, UVX, Change, fleet_executable


def mcp_entry(cmd: str) -> dict:
    """What we register. `fleet mcp` serves stdio, which is how clients launch a server."""
    if cmd == UVX:
        import shutil

        # Absolute when we can: a desktop client's PATH is not a shell's.
        return {"command": shutil.which("uvx") or "uvx", "args": ["agent-fleet", "mcp"]}
    return {"command": cmd, "args": ["mcp"]}



def apply_mcp(existing: str, key: str, cmd: str) -> str:
    """Add or update our server in a client's config, leaving every other byte alone.

    Same promise as the markdown region, kept a different way: the document is parsed,
    exactly one key is set, and it is written back. A config we cannot parse is returned
    untouched -- refusing to guess is the only safe move on a file we did not write and
    that may hold someone else's tokens.
    """
    import json

    try:
        doc = json.loads(existing) if existing.strip() else {}
    except json.JSONDecodeError:
        return existing
    if not isinstance(doc, dict):
        return existing
    servers = doc.get(key)
    if not isinstance(servers, dict):
        servers = {}
    servers["fleet"] = mcp_entry(cmd)
    doc[key] = servers
    return json.dumps(doc, indent=2) + "\n"



def remove_mcp(existing: str, key: str) -> str:
    """Drop our server. A config that never had one comes back untouched."""
    import json

    try:
        doc = json.loads(existing) if existing.strip() else {}
    except json.JSONDecodeError:
        return existing
    if not isinstance(doc, dict) or not isinstance(doc.get(key), dict):
        return existing
    if doc[key].pop("fleet", None) is None:
        return existing
    return json.dumps(doc, indent=2) + "\n"



def detect_mcp_clients(root: Path, platform: str = "") -> list[str]:
    """MCP clients that have actually run here. Judged by the directory their config
    lives in, because the file itself does not exist until one is configured."""
    platform = platform or sys.platform
    out = []
    for c in MCP_CLIENTS:
        path = c.path(root, platform)
        if path and path.parent.is_dir():
            out.append(c.name)
    return out



def installed_mcp_clients(root: Path, platform: str = "") -> list[str]:
    """Clients that already have fleet registered -- the only ones a refresh touches."""
    platform = platform or sys.platform
    out = []
    for c in MCP_CLIENTS:
        path = c.path(root, platform)
        if not path or not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get(c.key), dict) \
                and "fleet" in doc[c.key]:
            out.append(c.name)
    return out


def stale_mcp_clients(root: Path, cmd: str, platform: str = "") -> list[str]:
    """Registered clients whose entry points somewhere other than this fleet."""
    platform = platform or sys.platform
    out = []
    for c in MCP_CLIENTS:
        if c.name not in installed_mcp_clients(root, platform):
            continue
        path = c.path(root, platform)
        current = path.read_text(encoding="utf-8")
        if apply_mcp(current, c.key, cmd) != current:
            out.append(c.name)
    return out


def install_mcp(root: Path, clients: list[str], cmd: str, *,
                dry_run: bool = False, platform: str = "") -> list[Change]:
    """Register `fleet mcp` with each client, without disturbing its other servers."""
    platform = platform or sys.platform
    changes: list[Change] = []
    for client in MCP_CLIENTS:
        if client.name not in clients:
            continue
        path = client.path(root, platform)
        if path is None:
            continue
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        desired = apply_mcp(current, client.key, cmd)
        if desired == current and current.strip() and "fleet" not in current:
            # apply_mcp returns the file untouched when it cannot parse it, which is the
            # right thing to do to someone else's config and the wrong thing to report
            # as "unchanged" -- that reads as already set up.
            changes.append(Change(client.name, path, "unreadable"))
            continue
        action = ("unchanged" if desired == current
                  else "created" if not current else "updated")
        if action != "unchanged" and not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            replace_file(path, desired)
        changes.append(Change(client.name, path, action))
    return changes



def uninstall_mcp(root: Path, clients: list[str], *,
                  dry_run: bool = False, platform: str = "") -> list[Change]:
    platform = platform or sys.platform
    changes: list[Change] = []
    for client in MCP_CLIENTS:
        if client.name not in clients:
            continue
        path = client.path(root, platform)
        if path is None or not path.exists():
            continue
        current = path.read_text(encoding="utf-8")
        desired = remove_mcp(current, client.key)
        action = "removed" if desired != current else "unchanged"
        if action == "removed" and not dry_run:
            replace_file(path, desired)
        changes.append(Change(client.name, path, action))
    return changes

