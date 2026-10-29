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
        return {"command": shutil.which("uvx") or "uvx", "args": ["agents-fleet", "mcp"]}
    return {"command": cmd, "args": ["mcp"]}



def _parse(existing: str):
    """A client's config as (document, style), or None when it is not JSON we can edit.

    Windows editors save a byte-order mark, which `json.loads` refuses, and CRLF line
    ends. Both are put back on the way out: they are bytes we did not write.
    """
    bom = "\ufeff" if existing.startswith("\ufeff") else ""
    try:
        doc = json.loads(existing[len(bom):]) if existing.strip("\ufeff \t\r\n") else {}
    except json.JSONDecodeError:
        return None
    style = (bom, "\r\n" if "\r\n" in existing else "\n")
    return (doc, style) if isinstance(doc, dict) else None


def _dump(doc: dict, style: tuple[str, str]) -> str:
    bom, newline = style
    return bom + (json.dumps(doc, indent=2) + "\n").replace("\n", newline)


def _ours(entry) -> bool:
    """Whether a server named "fleet" is one fleet registered, not the user's own.

    Ours runs `<fleet> mcp` or `uvx agents-fleet mcp` (`agent-fleet` before the rename):
    a command or package naming fleet, serving `mcp`. Anything else under that name is
    someone's server, and replacing it would remove it from their client without a word.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("args"), list):
        return False
    args = [str(a) for a in entry["args"]]
    return bool(args) and args[-1] == "mcp" and any(
        "fleet" in Path(str(w).replace("\\", "/")).name.lower()
        for w in [entry.get("command", ""), *args[:-1]])


def mcp_state(existing: str, key: str, cmd: str | None = None) -> str:
    """Whether our entry can go in: `ok`, `unreadable` when the file is not JSON we can
    edit, or `taken` when a server of the user's own already goes by the name fleet."""
    parsed = _parse(existing)
    if parsed is None:
        return "unreadable"
    servers = parsed[0].get(key)
    if isinstance(servers, dict) and "fleet" in servers and not _ours(servers["fleet"]) \
            and (cmd is None or servers["fleet"] != mcp_entry(cmd)):
        return "taken"
    return "ok"


def apply_mcp(existing: str, key: str, cmd: str) -> str:
    """Add or update our server in a client's config, leaving every other byte alone.

    Same promise as the markdown region, kept a different way: the document is parsed,
    exactly one key is set, and it is written back. A config we cannot parse is returned
    untouched -- refusing to guess is the only safe move on a file we did not write and
    that may hold someone else's tokens. So is one where "fleet" names a server of the
    user's own.
    """
    if mcp_state(existing, key, cmd) != "ok":
        return existing
    doc, style = _parse(existing)
    servers = doc.get(key)
    if not isinstance(servers, dict):
        servers = {}
    servers["fleet"] = mcp_entry(cmd)
    doc[key] = servers
    return _dump(doc, style)



def remove_mcp(existing: str, key: str) -> str:
    """Drop our server. A config that never had one comes back untouched, and so does a
    server of the user's own that happens to be called fleet."""
    parsed = _parse(existing)
    if parsed is None:
        return existing
    doc, style = parsed
    if not isinstance(doc.get(key), dict) or not _ours(doc[key].get("fleet")):
        return existing
    del doc[key]["fleet"]
    return _dump(doc, style)


def _read(path: Path) -> str | None:
    """A config's text, or None when it cannot be read as UTF-8.

    Never an exception: this runs inside the first run of every command, and a client
    config saved in another encoding raised out of it before the first run could mark
    itself done -- so every later command, `fleet ls` included, failed the same way.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None



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
        parsed = _parse(_read(path) or "")
        if parsed is None:
            continue
        servers = parsed[0].get(c.key)
        if isinstance(servers, dict) and _ours(servers.get("fleet")):
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
        current = _read(path)
        if current is not None and apply_mcp(current, c.key, cmd) != current:
            out.append(c.name)
    return out


def install_mcp(root: Path, clients: list[str], cmd: str, *,
                dry_run: bool = False, platform: str = "", project: bool = False) -> list[Change]:
    """Register `fleet mcp` with each client, without disturbing its other servers."""
    platform = platform or sys.platform
    changes: list[Change] = []
    for client in MCP_CLIENTS:
        if client.name not in clients:
            continue
        path = client.path(root, platform, project=project)
        if path is None:
            continue
        current = _read(path) if path.exists() else ""
        state = "unreadable" if current is None else mcp_state(current, client.key, cmd)
        if state != "ok":
            # apply_mcp returns the file untouched then, which is the right thing to do
            # to someone else's config and the wrong thing to report as "unchanged" --
            # that reads as already set up. (Telling them apart by whether the text
            # said "fleet" anywhere called a broken config naming a fleet path fine.)
            changes.append(Change(client.name, path, state))
            continue
        desired = apply_mcp(current, client.key, cmd)
        action = ("unchanged" if desired == current
                  else "created" if not current else "updated")
        if action != "unchanged" and not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            replace_file(path, desired)
        changes.append(Change(client.name, path, action))
    return changes



def uninstall_mcp(root: Path, clients: list[str], *,
                  dry_run: bool = False, platform: str = "", project: bool = False) -> list[Change]:
    platform = platform or sys.platform
    changes: list[Change] = []
    for client in MCP_CLIENTS:
        if client.name not in clients:
            continue
        path = client.path(root, platform, project=project)
        if path is None or not path.exists():
            continue
        current = _read(path)
        if current is None or _parse(current) is None:
            # Left as found, which may still hold our entry: say so rather than
            # "unchanged", which reads as nothing of ours was there.
            changes.append(Change(client.name, path, "unreadable"))
            continue
        desired = remove_mcp(current, client.key)
        action = "removed" if desired != current else "unchanged"
        if action == "removed" and not dry_run:
            replace_file(path, desired)
        changes.append(Change(client.name, path, action))
    return changes

