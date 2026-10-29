"""The machines this machine looks at, stored as hand-editable YAML.

One plain file, `machines.yaml` in fleet's config folder: the machines you added -- a
rental by the ssh command its provider gave you, a box on the LAN, one found in your ssh
config or on your tailnet that you chose to watch. Nothing else writes it and nothing
copies it anywhere; share it between your machines with your dotfiles if you like.
What a machine says about itself lives on that machine (notes.py), not here.

Why YAML and not the SQLite cache: this is authored truth -- a couple of dozen records
you will edit and diff. It must remain readable when the tool is broken and fixable in
vim. Telemetry lives in the cache, so "delete cache.db and re-probe" is always safe.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from . import untrusted
from .writes import QueueTimeout, atomic_write, turn

from ..config import INVENTORY_PATH, ensure_dirs
from ..models import Device, Kind
from ..ssh.cmd import Endpoint, is_default_identity, route_of, same_key

SCHEMA_VERSION = 1


class InventoryError(RuntimeError):
    pass


def _lock(path: Path):
    """Writes take their turn, like every other state file (see state/writes.py)."""
    return turn(path)


def _identity(raw: str) -> str:
    """An identity path, but only if it exists here.

    `identity` is a filename on whichever machine recorded the endpoint, and the file may
    be shared with your dotfiles. Handing ssh `-i /Users/lin/.ssh/id_ed25519` on Windows
    fails the whole connection with "not accessible: No such file or directory" -- so a
    path this machine does not have is worse than no path at all, which leaves ssh to
    choose a key as it would for you.
    """
    path = os.path.expanduser(raw or "")
    if not path or not Path(path).exists():
        return ""
    # A key ssh offers anyway, which fleet recorded for nearly every machine before it
    # kept only an `-i` you typed: pinned, it shut out the keys in your agent.
    return "" if is_default_identity(path) else path


def _via(raw: str, target: str = "") -> str:
    """Normalise the route kind. See sshcmd.route_of for why it lives there."""
    return route_of(raw, target)


def _usable(e: dict) -> bool:
    """Whether an endpoint's fields can be handed to ssh as they are.

    The file may come from your dotfiles or another tool, so its addresses are not only
    ever typed here. A host, user or jump that began with `-` would be read by ssh as an option
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
    names: set[str] = set()
    for i, e in enumerate(dev.endpoints):
        if not _usable(e):
            continue
        # One name each: every route `fleet add` merged in was called "primary", so the
        # probe's record of which one answered named all of them.
        name = str(e.get("name") or f"ep{i}")
        if name in names:
            name = f"{name}-{i + 1}"
        names.add(name)
        out.append(Endpoint(
            target=e.get("target", ""), user=e.get("user", ""),
            port=int(e.get("port", 22) or 22), identity=_identity(e.get("identity", "")),
            jump=e.get("jump", "") or "", name=name,
            preference=int(e.get("preference", 10)),
            via=_via(e.get("via", ""), e.get("host") or e.get("target", "")),
            host=str(e.get("host", "") or ""),
        ))
    return out


def by_use(dev: Device, state: dict | None = None) -> list[Endpoint]:
    """The endpoints in preference order, with the one that answered the last probe first.

    The probe tries every route and keeps the first that answers, so a machine reached
    over its tailnet while its LAN address is down read as up -- and `fleet ssh` and the
    command `fleet show` printed went to the LAN address, and failed.
    """
    eps = sorted(endpoints_of(dev), key=lambda e: e.preference)
    used = (state or {}).get("endpoint_used") if (state or {}).get("status") == "ok" else ""
    if used and any(e.name == used for e in eps):
        eps.sort(key=lambda e: e.name != used)
    return eps


def load(path: Path | None = None) -> list[Device]:
    path = path or INVENTORY_PATH
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        # Report the line rather than read it as empty -- a stray tab must not blank
        # your fleet. `fleet` turns this into one line, not a traceback.
        raise InventoryError(f"{path} is not valid YAML: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise InventoryError(f"{path} is not UTF-8 text: {exc.reason} at byte "
                             f"{exc.start}") from exc
    except OSError as exc:
        raise InventoryError(f"{path} cannot be read: {exc.strerror or exc}") from exc
    try:
        return _devices_from(raw)
    except InventoryError as exc:
        # "device #2 in the inventory is malformed" did not say which file that is.
        raise InventoryError(f"{path}: {exc}") from exc


def stamp(path: Path | None = None) -> tuple | None:
    """What the list file is now, cheaply: it changes whenever any `fleet` writes it."""
    try:
        st = (path or INVENTORY_PATH).stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def _devices_from(raw: dict) -> list[Device]:
    """The records in a parsed inventory. A malformed one stops the load, by name.

    Not skipped: the next write would save the list without it, and a typo in one entry
    would quietly delete that machine from the fleet.
    """
    if not isinstance(raw, dict):
        raise InventoryError("not an inventory document")
    devices = []
    for i, d in enumerate(raw.get("devices") or []):
        try:
            devices.append(_device_from(d))
        except (TypeError, ValueError) as exc:
            label = d.get("name") if isinstance(d, dict) else None
            raise InventoryError(
                f"device {label or f'#{i + 1}'} in the inventory is malformed: {exc}") from exc
    # Two entries answering to one name -- typed by hand, or two lists merged in
    # dotfiles -- loaded without a word, and every command took the first: `fleet ssh
    # lab` reached the other machine, and `fleet rm lab` would have removed it.
    ids: set[str] = set()
    handles: set[str] = set()
    for d in live(devices):
        if d.id in ids:
            raise InventoryError(f"machine {d.id} is in the inventory twice (one as "
                                 f"{d.name}); remove one entry by hand")
        ids.add(d.id)
        for handle in dict.fromkeys(h for h in (d.name, d.alias) if h):
            if handle in handles:
                raise InventoryError(f"two machines in the inventory answer to {handle!r}; "
                                     "rename one by hand, or remove it")
            handles.add(handle)
    return devices


def _device_from(d) -> Device:
    if not isinstance(d, dict):
        raise TypeError("expected a mapping of fields")
    d = dict(d)
    # A file cut off mid-write, or an entry typed by hand: "Device.__init__() missing 1
    # required positional argument: 'name'" was the whole explanation.
    for key in ("id", "name"):
        if not d.get(key):
            raise ValueError(f"it has no {key}; fix the entry by hand, or remove it")
    # `name: 5` is YAML for a number: every view then failed in a traceback on an int
    # where text was expected.
    for key in ("id", "name", "alias"):
        if isinstance(d.get(key), (int, float)) and not isinstance(d[key], bool):
            d[key] = str(d[key])
        elif d.get(key) is not None and not isinstance(d[key], str):
            raise ValueError(f"{key} {d[key]!r} is not text")
    d["kind"] = Kind(d.get("kind", "permanent"))
    # Anything else was taken for on_demand: the machine was never probed, and `ls`
    # showed it as "?" with nothing said.
    if d.get("probe_policy") not in (None, "auto", "on_demand", "never"):
        raise ValueError(f"probe_policy {d['probe_policy']!r} is not auto, on_demand or "
                         "never")
    # `disk_paths: /data` -- no brackets -- was watched letter by letter: "disk path d is
    # not there".
    if isinstance(d.get("disk_paths"), str):
        d["disk_paths"] = [d["disk_paths"]]
    if d.get("disk_paths") is not None:
        if not isinstance(d["disk_paths"], list):
            raise ValueError(f"disk_paths {d['disk_paths']!r} is not a list of folders")
        d["disk_paths"] = [str(p) for p in d["disk_paths"] if p not in (None, "")]
    # `tags: gpu` -- no brackets -- is the likeliest typo in a file whose docstring
    # promises it stays fixable in vim, and without this it loads as the string
    # "gpu", which every consumer then iterates into ["g", "p", "u"].
    if isinstance(d.get("tags"), str):
        d["tags"] = [t for t in d["tags"].replace(",", " ").split() if t]
    # `tags: [gpu, 7]` and `cost: 1.5` are what YAML makes of a hand-typed number; every
    # view then failed on an int with no .strip(), or a float with no .get().
    if d.get("tags") is not None:
        if not isinstance(d["tags"], list):
            raise ValueError(f"tags {d['tags']!r} is not a list")
        d["tags"] = [str(t) for t in d["tags"] if t is not None]
    if d.get("cost") not in (None, {}):
        cost = d["cost"]
        if not isinstance(cost, dict):
            raise ValueError(f"cost {cost!r} is not `usd_per_hour: N`")
        if cost.get("usd_per_hour") is not None:
            try:
                usd = float(cost["usd_per_hour"])
            except (TypeError, ValueError):
                usd = float("nan")
            if not 0 <= usd < 1e6:
                raise ValueError(f"cost {cost['usd_per_hour']!r} is not US dollars per hour")
            d["cost"] = {**cost, "usd_per_hour": usd}
    # A port typed by hand (`port: 22a`) raised in every command that dialled out,
    # rather than naming the machine whose entry it is.
    for e in d.get("endpoints") or []:
        if not isinstance(e, dict):
            raise TypeError("each endpoint must be a mapping of fields")
        for field in ("port", "preference"):
            if e.get(field) not in (None, "") and not str(e[field]).strip().isdigit():
                raise ValueError(f"endpoint {e.get('target', '?')}: {field} "
                                 f"{e[field]!r} is not a number")
        # Port 99999 reached the probe as ssh's "Bad port", and port 0 was read as 22 by
        # `fleet ssh` but not by the probe.
        if e.get("port") not in (None, "") and not 1 <= int(str(e["port"]).strip()) <= 65535:
            raise ValueError(f"endpoint {e.get('target', '?')}: port {e['port']!r} is not a port "
                             "(1 to 65535)")
    known = {f for f in Device.__slots__}
    return Device(**{k: v for k, v in d.items() if k in known})


def loads(text: str) -> list[Device]:
    """Parse the same format the file holds, from text that came from elsewhere."""
    raw = untrusted.load(text)
    if not isinstance(raw, dict):
        raise InventoryError("not an inventory document")
    return _devices_from(raw)


def dumps(devices: list[Device]) -> str:
    return untrusted.dump(_payload(devices), sort_keys=False, allow_unicode=True, width=100)


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
    list it loaded minutes ago, which silently erases everything committed since -- an
    agent running `fleet add`, which probes for seconds, beside another's `fleet edit`.

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
    if not token:
        return None                        # "" is a prefix of every name
    if exact := find_exact(devices, token):
        return exact
    # The name it was added by: `fleet add lab` for a machine fleet already knew as `vm`
    # adds lab as another way in, and `fleet ssh lab` then said there was no such device.
    reached = [d for d in live(devices)
               if any(str(e.get("target", "")) == token for e in d.endpoints)]
    if len(reached) == 1:
        return reached[0]
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
    near = sorted(h for h in handles(devices)
                  if lowered in h.lower() or h.lower().startswith(lowered))
    if not near:
        # A slip of a key -- `nod1`, `ndoe1` -- found nothing to suggest: only a part of a
        # name did, so the one misspelt was answered with no name at all.
        import difflib

        lowers = {h.lower(): h for h in handles(devices)}
        near = [lowers[m] for m in difflib.get_close_matches(lowered, lowers, n=limit,
                                                              cutoff=0.75)]
    return near[:limit]


# How long a removed machine's record is kept, so a list shared between machines with
# your dotfiles can tell "removed here" from "not added there yet".
TOMBSTONE_TTL_S = 60 * 60 * 24 * 30


def live(devices: list[Device]) -> list[Device]:
    """The devices that still exist. Everything user-facing reads through this."""
    return [d for d in devices if not d.deleted_at]


def remove(devices: list[Device], dev: Device | None) -> None:
    """Mark a device deleted. The record stays a while, so a copy of the file on another
    machine that still lists it can be told apart from one that has not seen it yet."""
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
    """Stamp a mutation. Every command that edits a device calls this."""
    dev.updated_at = int(time.time())


def same_route(new: dict, old: dict) -> bool:
    """Whether endpoint `new` is a route already recorded as `old`.

    The address alone was the test, so `fleet add "ssh -J other-jump ..."` or `-i
    other-key` for a machine already known said "already recorded with this endpoint" and
    went on by the old jump host and the old key. A new route beside it, not over it: the
    old one may be how another of your machines reaches it. A key left unsaid is not a
    different key -- the one recorded may be a path on another machine."""
    def addr(e: dict) -> tuple:
        return (e.get("target"), e.get("user") or "", int(e.get("port", 22) or 22),
                e.get("jump") or "")
    return addr(new) == addr(old) and (
        not new.get("identity") or same_key(new["identity"], old.get("identity") or ""))


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
            # like it silently did nothing.
            restored = bool(existing.deleted_at)
            if restored:
                existing.deleted_at = 0
                touch(existing)
            added = [e for e in new.endpoints
                     if not any(same_route(e, old) for old in existing.endpoints)]
            if added:
                existing.endpoints.extend(added)
                touch(existing)
            if restored:
                return devices, "restored"
            return devices, "endpoint_added" if added else "unchanged"
    touch(new)
    devices.append(new)
    return devices, "added"
