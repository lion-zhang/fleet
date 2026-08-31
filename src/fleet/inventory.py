"""Durable device inventory, stored as hand-editable YAML.

Why YAML and not the SQLite cache: this is authored truth -- a couple of dozen records
you will edit, diff, and eventually sync. It must remain readable when the tool is
broken and fixable in vim. Telemetry lives in the cache, so "delete cache.db and
re-probe" is always safe advice.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from filelock import FileLock, Timeout

from .config import INVENTORY_PATH, ensure_dirs
from .models import Device, Kind
from .sshcmd import Endpoint

SCHEMA_VERSION = 1


class InventoryError(RuntimeError):
    pass


def _lock(path: Path) -> FileLock:
    return FileLock(str(path) + ".lock", timeout=10)


def endpoints_of(dev: Device) -> list[Endpoint]:
    out: list[Endpoint] = []
    for i, e in enumerate(dev.endpoints):
        out.append(Endpoint(
            target=e.get("target", ""), user=e.get("user", ""),
            port=int(e.get("port", 22) or 22), identity=os.path.expanduser(e.get("identity", "") or ""),
            jump=e.get("jump", "") or "", name=e.get("name", f"ep{i}"),
            preference=int(e.get("preference", 10)), via=e.get("via", "") or "",
        ))
    return out


def load(path: Path | None = None) -> list[Device]:
    path = path or INVENTORY_PATH
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        # Report the line and keep going -- a stray tab must not blank your fleet.
        raise InventoryError(f"{path} is not valid YAML: {exc}") from exc
    devices = []
    for d in raw.get("devices") or []:
        d = dict(d)
        d["kind"] = Kind(d.get("kind", "permanent"))
        known = {f for f in Device.__slots__}
        devices.append(Device(**{k: v for k, v in d.items() if k in known}))
    return devices


def save(devices: list[Device], path: Path | None = None) -> None:
    """Atomic, locked write. Two agents adding devices concurrently must not interleave."""
    path = path or INVENTORY_PATH
    ensure_dirs()
    payload = {
        "version": SCHEMA_VERSION,
        "devices": [
            {k: (v.value if isinstance(v, Kind) else v)
             for k, v in _as_dict(d).items() if v not in ("", [], {}, None, False) or k in ("name", "id")}
            for d in sorted(devices, key=lambda x: x.name)
        ],
    }
    try:
        with _lock(path):
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".inventory-", suffix=".yaml")
            try:
                with os.fdopen(fd, "w") as fh:
                    yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True, width=100)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise
    except Timeout as exc:
        raise InventoryError("another fleet process is holding the inventory lock") from exc


def _as_dict(d: Device) -> dict:
    return {f: getattr(d, f) for f in Device.__slots__}


def find(devices: list[Device], name_or_id: str) -> Device | None:
    for d in devices:
        if d.name == name_or_id or d.id == name_or_id:
            return d
    matches = [d for d in devices if d.name.startswith(name_or_id)]
    return matches[0] if len(matches) == 1 else None


def upsert(devices: list[Device], new: Device) -> tuple[list[Device], str]:
    """Add a device, or merge an endpoint into the one that already has this identity.

    The dedupe is the point: `oracle` answers as both root@ and ubuntu@, and a box on
    the tailnet is usually also reachable on the LAN. Those are one machine.
    """
    for existing in devices:
        if existing.id == new.id:
            known = {(e.get("target"), e.get("user"), int(e.get("port", 22) or 22))
                     for e in existing.endpoints}
            added = [e for e in new.endpoints
                     if (e.get("target"), e.get("user"), int(e.get("port", 22) or 22)) not in known]
            if not added:
                return devices, "unchanged"
            existing.endpoints.extend(added)
            return devices, "endpoint_added"
    devices.append(new)
    return devices, "added"
