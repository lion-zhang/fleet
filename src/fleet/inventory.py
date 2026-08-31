"""Durable device inventory, stored as hand-editable YAML.

Why YAML and not the SQLite cache: this is authored truth -- a couple of dozen records
you will edit, diff, and eventually sync. It must remain readable when the tool is
broken and fixable in vim. Telemetry lives in the cache, so "delete cache.db and
re-probe" is always safe advice.
"""

from __future__ import annotations

import os
import time
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


def touch(dev: Device) -> None:
    """Stamp a mutation. Every command that edits a device must call this, or the merge
    has nothing to break a tie with."""
    dev.updated_at = int(time.time())


def _endpoint_key(e: dict) -> tuple:
    return (e.get("target", ""), e.get("user", ""), int(e.get("port", 22) or 22))


def _union_endpoints(primary: list[dict], other: list[dict]) -> list[dict]:
    """Both sides' routes, deduped. A box reachable on the tailnet from one machine and
    on the LAN from another is one box with two routes -- the same dedupe upsert()
    performs at onboarding."""
    out = list(primary)
    seen = {_endpoint_key(e) for e in out}
    for e in other:
        if _endpoint_key(e) not in seen:
            seen.add(_endpoint_key(e))
            out.append(e)
    return out


def merge(local: list[Device], remote: list[Device]) -> tuple[list[Device], list[str]]:
    """Combine two inventories. Returns (merged, human-readable changes).

    Devices are matched on id, which is why id prefers machine-id over an address: two
    machines may have named the same box differently, and the address may since have
    changed. Newer updated_at wins the record; endpoints are unioned regardless, because
    a route one machine knows about is still a real route.

    Deletion is deliberately not synced. Telling "deleted here" apart from "not seen
    here yet" needs tombstones, and guessing wrong either resurrects a device or
    destroys one. `fleet rm` is local; remove on the center to remove for good.
    """
    by_id: dict[str, Device] = {d.id: d for d in local}
    changes: list[str] = []

    for incoming in remote:
        mine = by_id.get(incoming.id)
        if mine is None:
            by_id[incoming.id] = incoming
            changes.append(f"added {incoming.name}")
            continue
        endpoints = _union_endpoints(mine.endpoints, incoming.endpoints)
        gained = len(endpoints) - len(mine.endpoints)
        if incoming.updated_at > mine.updated_at:
            incoming.endpoints = _union_endpoints(incoming.endpoints, mine.endpoints)
            by_id[incoming.id] = incoming
            changes.append(f"updated {incoming.name}")
        elif gained:
            mine.endpoints = endpoints
        if gained:
            changes.append(f"{by_id[incoming.id].name}: +{gained} endpoint(s)")

    return sorted(by_id.values(), key=lambda d: d.name), changes


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
