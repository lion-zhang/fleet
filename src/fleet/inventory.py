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
    return _devices_from(raw)


def _devices_from(raw: dict) -> list[Device]:
    devices = []
    for d in raw.get("devices") or []:
        d = dict(d)
        d["kind"] = Kind(d.get("kind", "permanent"))
        known = {f for f in Device.__slots__}
        devices.append(Device(**{k: v for k, v in d.items() if k in known}))
    return devices


def loads(text: str) -> list[Device]:
    """Parse the same format the file holds. Sync ships inventories over a pipe, and
    two formats that can drift would be one format too many."""
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise InventoryError("not an inventory document")
    return _devices_from(raw)


def dumps(devices: list[Device]) -> str:
    return yaml.safe_dump(_payload(devices), sort_keys=False, allow_unicode=True, width=100)


def _payload(devices: list[Device]) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "devices": [
            {k: (v.value if isinstance(v, Kind) else v)
             for k, v in _as_dict(d).items()
             if v not in ("", [], {}, None, False) or k in ("name", "id")}
            for d in sorted(devices, key=lambda x: x.name)
        ],
    }


def save(devices: list[Device], path: Path | None = None) -> None:
    """Atomic, locked write. Two agents adding devices concurrently must not interleave."""
    path = path or INVENTORY_PATH
    ensure_dirs()
    payload = _payload(prune_tombstones(devices))
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
    devices = live(devices)
    for d in devices:
        if d.name == name_or_id or d.id == name_or_id:
            return d
    matches = [d for d in devices if d.name.startswith(name_or_id)]
    return matches[0] if len(matches) == 1 else None


TOMBSTONE_TTL_S = 60 * 60 * 24 * 30      # long enough for every machine to have synced


def live(devices: list[Device]) -> list[Device]:
    """The devices that still exist. Everything user-facing reads through this."""
    return [d for d in devices if not d.deleted_at]


def remove(devices: list[Device], dev: Device | None) -> None:
    """Mark a device deleted. The record stays so the deletion can propagate."""
    if dev is None:
        return
    dev.deleted_at = int(time.time())
    touch(dev)


def prune_tombstones(devices: list[Device]) -> list[Device]:
    """Drop tombstones old enough that every machine has certainly seen them. Without
    this the inventory only ever grows."""
    cutoff = int(time.time()) - TOMBSTONE_TTL_S
    return [d for d in devices if not d.deleted_at or d.deleted_at > cutoff]


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


def promote_center(devices: list[Device], new_center: Device) -> list[str]:
    """Make one device the center, demoting whoever held it.

    A demoted center becomes a *backup*, never "none": it still has fleet installed and
    still holds a full copy of your state, and dropping it to none would silently
    discard a replica. The demotion is stamped, or it would lose the next merge to the
    other machine's stale "center" record and you would be back to two centers.
    """
    changes: list[str] = []
    for d in live(devices):
        if d.role == "center" and d.id != new_center.id:
            d.role = "backup"
            touch(d)
            changes.append(f"{d.name}: center -> backup")
    if new_center.role != "center":
        new_center.role = "center"
        touch(new_center)
        changes.append(f"{new_center.name}: -> center")
    return changes


def _one_center(devices: list[Device]) -> None:
    """Two machines can each promote a different device before syncing. Left alone,
    `fleet sync` would then pick a center arbitrarily, so the newest promotion wins and
    the rest fall back to backup."""
    centers = [d for d in live(devices) if d.role == "center"]
    if len(centers) < 2:
        return
    keep = max(centers, key=lambda d: d.updated_at)
    for d in centers:
        if d is not keep:
            d.role = "backup"


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

    merged = sorted(by_id.values(), key=lambda d: d.name)
    _one_center(merged)
    return merged, changes


def upsert(devices: list[Device], new: Device) -> tuple[list[Device], str]:
    """Add a device, or merge an endpoint into the one that already has this identity.

    The dedupe is the point: `oracle` answers as both root@ and ubuntu@, and a box on
    the tailnet is usually also reachable on the LAN. Those are one machine.
    """
    for existing in devices:
        if existing.id == new.id:
            # Re-adding something you removed is an ordinary correction. Because the
            # match is on id, without clearing the tombstone the add would merge into a
            # deleted record and the device would stay invisible -- `fleet add` looking
            # like it silently did nothing. Stamping it also matters: an unstamped
            # restore loses the next merge to the center's stale tombstone and the
            # device is deleted straight back.
            restored = bool(existing.deleted_at)
            if restored:
                existing.deleted_at = 0
                touch(existing)
            known = {(e.get("target"), e.get("user"), int(e.get("port", 22) or 22))
                     for e in existing.endpoints}
            added = [e for e in new.endpoints
                     if (e.get("target"), e.get("user"), int(e.get("port", 22) or 22)) not in known]
            if added:
                existing.endpoints.extend(added)
            if restored:
                return devices, "restored"
            return devices, "endpoint_added" if added else "unchanged"
    devices.append(new)
    return devices, "added"
