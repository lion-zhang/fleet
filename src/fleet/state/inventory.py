"""Durable device inventory, stored as hand-editable YAML.

Why YAML and not the SQLite cache: this is authored truth -- a couple of dozen records
you will edit, diff, and eventually sync. It must remain readable when the tool is
broken and fixable in vim. Telemetry lives in the cache, so "delete cache.db and
re-probe" is always safe advice.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from .writes import QueueTimeout, atomic_write, turn

from ..config import INVENTORY_PATH, ensure_dirs
from ..models import Device, Kind
from ..ssh.cmd import Endpoint, route_of

SCHEMA_VERSION = 1


class InventoryError(RuntimeError):
    pass


def _lock(path: Path):
    """Writes take their turn, like every other state file (see state/writes.py)."""
    return turn(path)


def _identity(raw: str) -> str:
    """An identity path, but only if it exists here.

    `identity` is a filename on whichever machine recorded the endpoint, and the
    inventory syncs. Handing ssh `-i /Users/lin/.ssh/id_ed25519` on a Windows center
    fails the whole connection with "not accessible: No such file or directory" -- so a
    path this machine does not have is worse than no path at all, which just falls back
    to the fleet key.
    """
    path = os.path.expanduser(raw or "")
    return path if path and Path(path).exists() else ""


def _via(raw: str, target: str = "") -> str:
    """Normalise the route kind. See sshcmd.route_of for why it lives there."""
    return route_of(raw, target)


def _usable(e: dict) -> bool:
    """Whether an endpoint's fields can be handed to ssh as they are.

    The inventory is shared between machines, so its addresses are not only ever typed
    by you. A host, user or jump that began with `-` would be read by ssh as an option
    (older OpenSSH, as some Windows builds ship, accepts more of them), and whitespace or
    a control character has no business in any of the three. Such an endpoint is left
    out rather than dialled.
    """
    for field in ("target", "user", "jump"):
        v = str(e.get(field, "") or "")
        if v.startswith("-") or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in v):
            return False
    return bool(e.get("target"))


def endpoints_of(dev: Device) -> list[Endpoint]:
    out: list[Endpoint] = []
    for i, e in enumerate(dev.endpoints):
        if not _usable(e):
            continue
        out.append(Endpoint(
            target=e.get("target", ""), user=e.get("user", ""),
            port=int(e.get("port", 22) or 22), identity=_identity(e.get("identity", "")),
            jump=e.get("jump", "") or "", name=e.get("name", f"ep{i}"),
            preference=int(e.get("preference", 10)),
            via=_via(e.get("via", ""), e.get("target", "")),
        ))
    return out


def load(path: Path | None = None) -> list[Device]:
    path = path or INVENTORY_PATH
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        # Report the line and keep going -- a stray tab must not blank your fleet.
        raise InventoryError(f"{path} is not valid YAML: {exc}") from exc
    return _devices_from(raw)


def _devices_from(raw: dict) -> list[Device]:
    devices = []
    for d in raw.get("devices") or []:
        d = dict(d)
        d["kind"] = Kind(d.get("kind", "permanent"))
        # `tags: gpu` -- no brackets -- is the likeliest typo in a file whose docstring
        # promises it stays fixable in vim, and without this it loads as the string
        # "gpu", which every consumer then iterates into ["g", "p", "u"].
        if isinstance(d.get("tags"), str):
            d["tags"] = [t for t in d["tags"].replace(",", " ").split() if t]
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


def _write(devices: list[Device], path: Path) -> None:
    """Atomic replace. The caller must already hold the turn."""
    atomic_write(path, yaml.safe_dump(_payload(prune_tombstones(devices)), sort_keys=False,
                                      allow_unicode=True, width=100))


def save(devices: list[Device], path: Path | None = None) -> None:
    """Atomic, locked write. Two agents adding devices concurrently must not interleave."""
    path = path or INVENTORY_PATH
    ensure_dirs()
    try:
        with _lock(path):
            _write(devices, path)
    except QueueTimeout as exc:
        raise InventoryError("another fleet process is holding the inventory lock") from exc


def update(mutate, path: Path | None = None):
    """Load, apply `mutate`, and write back while holding the lock the whole time.

    save() prevents two writers interleaving; it does nothing about a writer holding a
    list it loaded minutes ago, which silently erases everything committed since. Sync
    is exactly that writer -- it carries a snapshot across an SSH round trip -- and so
    is any agent running `fleet add` or `fleet edit` beside another.

    `mutate(devices) -> (devices_to_write, result)`; returns (written, result).
    """
    path = path or INVENTORY_PATH
    ensure_dirs()
    try:
        with _lock(path):
            devices, result = mutate(load(path))
            _write(devices, path)
            return devices, result
    except QueueTimeout as exc:
        raise InventoryError("another fleet process is holding the inventory lock") from exc


def _as_dict(d: Device) -> dict:
    return {f: getattr(d, f) for f in Device.__slots__}


def find(devices: list[Device], token: str) -> Device | None:
    """Exact name, alias or id, else a unique name prefix. For everyday commands."""
    if exact := find_exact(devices, token):
        return exact
    matches = [d for d in live(devices) if d.name.startswith(token)]
    return matches[0] if len(matches) == 1 else None


def find_exact(devices: list[Device], token: str) -> Device | None:
    """No prefix fallback. For commands whose mistake cannot be undone.

    An alias counts as exact: it is a name the user chose for this machine and typed in
    full, not a fragment guessed at. What this rules out is the prefix, which `find`
    still resolves -- right for `show`, `ssh` and `top`, where a wrong guess costs one
    re-run, and wrong for `rm`, where `fleet rm lin` would remove `lin-xps` on the
    strength of three letters.
    """
    for d in live(devices):
        if token in (d.name, d.id) or (d.alias and d.alias == token):
            return d
    return None


def handles(devices: list[Device], *, excluding: str = "") -> set[str]:
    """Every string that already names a machine. Aliases share the namespace with
    names, so `--alias` cannot shadow another machine's name and vice versa."""
    taken: set[str] = set()
    for d in live(devices):
        if excluding and d.id == excluding:
            continue
        taken.add(d.name)
        if d.alias:
            taken.add(d.alias)
    return taken


def near_matches(devices: list[Device], name: str, limit: int = 5) -> list[str]:
    """Handles worth suggesting after `find_exact` came back empty."""
    lowered = name.lower()
    return sorted(h for h in handles(devices)
                  if lowered in h.lower() or h.lower().startswith(lowered))[:limit]


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
    """Make one device the center, demoting whoever held it to none.

    A demoted center becomes "none". It used to become a backup, which meant a
    second machine holding a key on every device forever -- a standing
    total-compromise target, to save an occasional manual recovery.
    still holds a full copy of your state, and dropping it to none would silently
    discard a replica. The demotion is stamped, or it would lose the next merge to the
    other machine's stale "center" record and you would be back to two centers.
    """
    changes: list[str] = []
    for d in live(devices):
        if d.role == "center" and d.id != new_center.id:
            # Demoted to none, not to a second-root role. A former center that keeps a
            # key on every device can still open every door, permanently, to save an
            # occasional manual recovery. There is exactly one fleet-root.
            d.role = "none"
            touch(d)
            changes.append(f"{d.name}: center -> none")
    if new_center.role != "center":
        new_center.role = "center"
        touch(new_center)
        changes.append(f"{new_center.name}: -> center")
    return changes


def _one_center(devices: list[Device]) -> None:
    """Two machines can each promote a different device before syncing. Left alone,
    `fleet sync` would then pick a center arbitrarily, so the newest promotion wins and
    the rest fall back to none."""
    centers = [d for d in live(devices) if d.role == "center"]
    if len(centers) < 2:
        return
    keep = max(centers, key=lambda d: d.updated_at)
    for d in centers:
        if d is not keep:
            d.role = "none"


# What a member may change about a machine other than itself. Labels a person curates,
# and nothing that decides where fleet connects or who is the center.
MEMBER_MAY_EDIT = ("tags", "cost", "disk_paths", "notes")


def from_member(current: list[Device], incoming: list[Device], sender_id: str) -> list[Device]:
    """The part of a member's inventory the center takes, before it is merged.

    The center merges what members send -- through its listener and from every sweep --
    and then signs the result and hands it to everyone as authoritative. Merged as it
    came, any member could rewrite any machine's record: a future `updated_at` won the
    whole record, an extra endpoint with a low preference became the route every machine
    dials for `fleet ssh`, and the sweep then "revoked" keys on whatever host answered
    there while the ledger reported the revoke as done. So a member is the authority on
    itself and on machines nobody has recorded yet (adding from a member is allowed),
    and for the rest may change only the labels in MEMBER_MAY_EDIT. Never a role: no
    machine makes itself, or anyone, the center. A deletion counts only for itself.
    """
    import dataclasses

    now = int(time.time())
    have = {d.id: d for d in current}
    out: list[Device] = []
    for d in incoming:
        mine = have.get(d.id)
        if d.deleted_at and d.id != sender_id:
            continue
        # A future clock wins nothing: newer than ours counts as newer by a second, not
        # by however far ahead the member's clock happens to be.
        cap = max(now, mine.updated_at + 1) if mine is not None else now
        d.updated_at = min(int(d.updated_at or 0), cap)
        if d.id == sender_id or mine is None:
            d.role = mine.role if mine is not None else "none"
            out.append(d)
            continue
        if d.updated_at <= mine.updated_at:
            continue
        out.append(dataclasses.replace(
            mine, updated_at=d.updated_at,
            **{f: getattr(d, f) for f in MEMBER_MAY_EDIT}))
    return out


def merge(local: list[Device], remote: list[Device], *,
          authoritative: bool = False) -> tuple[list[Device], list[str]]:
    """Combine two inventories. Returns (merged, human-readable changes).

    Devices are matched on id, which is why id prefers machine-id over an address: two
    machines may have named the same box differently, and the address may since have
    changed. Newer updated_at wins the record; an older one changes nothing. When the
    newer record is not the authority, its endpoints are unioned with ours.

    Deletion travels as a tombstone: `fleet rm` keeps the record with `deleted_at` set,
    and it wins like any newer record, so "deleted here" is never mistaken for "not seen
    here yet". The listener accepts a member's tombstone only for that member itself
    (serve.py); tombstones are pruned after TOMBSTONE_TTL_S.
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
            # Unioning both ways means an endpoint can be added but never removed: a
            # route one machine learned survives forever, on every machine, however
            # wrong it turns out to be. When the incoming side is the authority -- the
            # center's signed answer -- its list replaces ours, so editing there is a
            # way to take a route away rather than only to add one.
            if not authoritative:
                incoming.endpoints = _union_endpoints(incoming.endpoints, mine.endpoints)
            by_id[incoming.id] = incoming
            changes.append(f"updated {incoming.name}")
        elif incoming.updated_at == mine.updated_at:
            # The same version of the record, reached two ways. Routes either side
            # holds are both real -- unless the other side is the center's signed list,
            # which then says exactly which routes there are.
            mine.endpoints = list(incoming.endpoints) if authoritative else endpoints
        else:
            # An older record adds nothing -- not even a route. Taking its endpoints
            # anyway was how a route removed or changed on the center came back: a
            # member still holding the old one synced, the center unioned it into its
            # newer record, signed the result and handed it to everyone. A real new
            # route arrives with a newer record (`fleet add` stamps it).
            gained = 0
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
                touch(existing)            # or the new route loses the next merge
            if restored:
                return devices, "restored"
            return devices, "endpoint_added" if added else "unchanged"
    devices.append(new)
    return devices, "added"
