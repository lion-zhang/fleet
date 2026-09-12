"""Change a device after it has been onboarded.

Addresses are load-bearing here. A device we never probed successfully has no
machine-id, so `net:<host>:<port>` is not merely where it lives -- it is who it is.
Re-addressing such a device therefore changes its identity, and the caller has to move
the cache rows keyed by the old one. A probed device keeps its machine-id and simply
points somewhere new, which is the entire reason machine-id is preferred at onboarding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .inventory import touch
from .models import Device
from .sshcmd import Endpoint


@dataclass(slots=True)
class Edits:
    """What an edit did. `previous_id` is set only when the identity actually moved."""

    changes: list[str] = field(default_factory=list)
    previous_id: str | None = None


def _address(user: str, target: str, port: int) -> str:
    return f"{user}@{target}:{port}" if user else f"{target}:{port}"


# "tailscale" is the pre-rename spelling and is still accepted on read: inventories
# written before `via` was de-vendored are on disk right now, and treating those routes
# as direct would rewrite the one address that does not move -- the exact failure the
# preference for a non-overlay route exists to prevent.
_OVERLAY = frozenset({"mesh", "tailscale"})


def _replace_primary(dev: Device, ep: Endpoint, out: Edits) -> None:
    """Repoint the endpoint the pasted address refers to.

    Prefer a direct route over a tailnet one. A tailnet name is stable -- it is the
    public or LAN address that churns when a rental is recycled -- so replacing the
    overlay route with a raw IP would throw away the more durable way in. Fall back
    to the most-preferred endpoint when every route is a tailnet one, because an edit
    must never silently do nothing.
    """
    record: dict = {"target": ep.target, "user": ep.user, "port": ep.port}
    if ep.identity:
        record["identity"] = ep.identity
    if ep.jump:
        record["jump"] = ep.jump

    if not dev.endpoints:
        dev.endpoints = [record | {"name": "default", "preference": 10}]
        out.changes.append(f"address -> {_address(ep.user, ep.target, ep.port)}")
        return

    eps = list(dev.endpoints)
    direct = [i for i, e in enumerate(eps) if (e.get("via") or "") not in _OVERLAY]
    idx = min(direct or range(len(eps)), key=lambda i: int(eps[i].get("preference", 10) or 10))
    old = eps[idx]
    before = _address(old.get("user", ""), old.get("target", ""), int(old.get("port", 22) or 22))
    after = _address(ep.user, ep.target, ep.port)
    record["name"] = old.get("name", "default")
    record["preference"] = int(old.get("preference", 10) or 10)
    if old.get("via"):
        record["via"] = old["via"]
    eps[idx] = record
    dev.endpoints = eps
    if before != after:
        out.changes.append(f"address {before} -> {after}")


def _migrate_identity(dev: Device, ep: Endpoint, out: Edits) -> None:
    if not dev.id.startswith("net:"):
        return                              # a real machine-id outlives any address
    new_id = f"net:{ep.target}:{ep.port}"
    if new_id == dev.id:
        return
    out.previous_id = dev.id
    out.changes.append(f"id {dev.id} -> {new_id} "
                       "(never probed, so its address was its identity)")
    dev.id = new_id


def apply_edits(dev: Device, *, endpoint: Endpoint | None = None,
                disk_paths: list[str] | None = None, role: str | None = None) -> Edits:
    out = Edits()
    if role is not None and role != dev.role:
        out.changes.append(f"role {dev.role} -> {role}")
        dev.role = role
    if endpoint is not None:
        _replace_primary(dev, endpoint, out)
        _migrate_identity(dev, endpoint, out)
    if disk_paths is not None and list(disk_paths) != list(dev.disk_paths):
        before = list(dev.disk_paths) or ["auto"]
        dev.disk_paths = list(disk_paths)
        out.changes.append(f"disk paths {before} -> {dev.disk_paths or ['auto']}")
    if out.changes:
        # only a real change stamps: a no-op edit must not make this machine's copy
        # spuriously win the next merge.
        touch(dev)
    return out
