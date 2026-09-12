"""Who may reach whom, and the one machine that decides.

The list is not a permission check. sshd enforces access, from `authorized_keys`, and
fleet is not in that path at all -- so this file is the *input to a reconciler*, and the
property that actually holds is "only the center can cause a key to appear on another
machine", which is structural rather than declared.

Three things follow, and they are why this is not a field on Device:

* It must not ride `inventory.merge`, which is whole-record last-writer-wins on
  `updated_at` against unsynchronised clocks. "I grant myself everything, timestamped
  next Tuesday" would win.
* `inventory._payload` drops falsy values, so "may reach nothing" could not round-trip.
* An edge needs `pending_since`, `attempts` and `last_error` to be able to say that a
  revoke has *not* taken effect yet, which is the difference between this and a note.

Edges are keyed on the **fingerprint of a machine's fleet key**, not its device id.
Device ids are not stable: `derive_id` returns `net:<target>:<port>` when a probe fails
-- exactly the awaiting-enrollment case this design creates -- and `edit` rewrites it
later. A fingerprint is stable by construction, because the key is the identity.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import CONFIG_DIR, FLEET_KEY, STATE_DIR

# CONFIG_DIR and STATE_DIR are the *same directory* on macOS (platformdirs gives both as
# ~/Library/Application Support/fleet). So every name here is globally distinct, and
# nothing may ever be deleted by globbing a directory -- `--leave` tidying up "the state
# dir" would otherwise take the center's own authority file with it.
ACCESS_PATH = CONFIG_DIR / "access.yaml"           # authority. center only
LEDGER_PATH = STATE_DIR / "access-ledger.yaml"     # desired vs observed. center only
CACHE_PATH = STATE_DIR / "access-cache.yaml"       # signed copy of our row. spokes
OUTBOX_PATH = STATE_DIR / "access-outbox.yaml"     # requests we have filed

SIGN_NAMESPACE = "fleet-access"
VERSION = 1


class AccessError(RuntimeError):
    """Anything that would otherwise silently become "no access anywhere"."""


def fingerprint(pubkey: str) -> str:
    """OpenSSH's own SHA256 fingerprint, computed rather than shelled out for.

    Same string `ssh-keygen -lf` prints, so it can be compared against anything a human
    reads out of an authorized_keys file.
    """
    parts = (pubkey or "").split()
    if len(parts) < 2:
        raise AccessError(f"not a public key: {pubkey[:40]!r}")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except Exception as exc:
        raise AccessError(f"unreadable public key: {exc}") from exc
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


@dataclass(slots=True)
class Edge:
    """One grant. `user` is part of the identity: a box answers as both root@ and
    ubuntu@, and "remove A's key from B" is ambiguous without saying whose file."""

    src: str
    dst: str
    user: str = "root"
    granted_at: int = field(default_factory=lambda: int(time.time()))
    note: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.user)


@dataclass(slots=True)
class Access:
    fleet_id: str = ""
    center: str = ""                       # fingerprint of the center's key
    generation: int = 0
    keys: dict = field(default_factory=dict)     # fingerprint -> {name, pubkey, device_id}
    allow: list = field(default_factory=list)    # list[Edge]

    def edges(self) -> set[tuple[str, str, str]]:
        """Every edge that should exist, including the center's implicit ones.

        The center's reach is computed, never written: promoting a new center would
        otherwise mean rewriting every row, and a hand-edited file could accidentally
        omit the one edge that makes the fleet manageable at all.
        """
        out = {e.key for e in self.allow}
        if self.center:
            for fp, meta in self.keys.items():
                if fp != self.center:
                    out.add((self.center, fp, meta.get("user", "root")))
        return out

    def name_of(self, fp: str) -> str:
        return (self.keys.get(fp) or {}).get("name") or fp[:18]


def load(path: Path | None = None) -> Access:
    """Read the access list. **Raises** if it is missing or unreadable.

    Deliberately unlike `inventory.load`, which returns [] for a missing file. An empty
    list here does not mean "no devices yet" -- it means every edge should be removed,
    and a reconciler acting on it would strip the center's own key from every machine in
    one sweep, permanently. Absence must be an error, never a value.
    """
    path = path or ACCESS_PATH
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as exc:
        raise AccessError(
            f"no access list at {path} -- this machine is not a center") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise AccessError(f"unreadable access list at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AccessError(f"access list at {path} is not a mapping")
    return Access(
        fleet_id=str(raw.get("fleet_id") or ""),
        center=str(raw.get("center") or ""),
        generation=int(raw.get("generation") or 0),
        keys=dict(raw.get("keys") or {}),
        allow=[Edge(src=e["from"], dst=e["to"], user=e.get("user", "root"),
                    granted_at=int(e.get("granted_at") or 0), note=e.get("note", ""))
               for e in (raw.get("allow") or [])],
    )


def dumps(acc: Access) -> str:
    return yaml.safe_dump({
        "version": VERSION,
        "fleet_id": acc.fleet_id,
        "center": acc.center,
        "generation": acc.generation,
        "keys": acc.keys,
        "allow": [{"from": e.src, "to": e.dst, "user": e.user,
                   "granted_at": e.granted_at, **({"note": e.note} if e.note else {})}
                  for e in acc.allow],
    }, sort_keys=False)


def save(acc: Access, path: Path | None = None) -> None:
    """Write atomically, bumping the generation so a stale copy is recognisable."""
    path = path or ACCESS_PATH
    acc.generation += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dumps(acc))
    os.replace(tmp, path)


def grant(acc: Access, src: str, dst: str, *, user: str = "root", note: str = "") -> bool:
    """Add an edge. False if it was already there."""
    if (src, dst, user) in {e.key for e in acc.allow}:
        return False
    acc.allow.append(Edge(src=src, dst=dst, user=user, note=note))
    return True


def revoke(acc: Access, src: str, dst: str, *, user: str = "root") -> bool:
    """Remove an edge. Refuses to touch the center's.

    The center's reach is what makes every other operation possible; revoking it would
    leave a machine no center could ever write to again, and there is no recovery path
    from that. It is not expressible rather than merely discouraged.
    """
    if src == acc.center:
        raise AccessError(
            f"{acc.name_of(src)} is the center -- revoking its own access would strand "
            f"{acc.name_of(dst)} with no way back")
    before = len(acc.allow)
    acc.allow = [e for e in acc.allow if e.key != (src, dst, user)]
    return len(acc.allow) != before


def resolve(acc: Access, name_or_fp: str) -> str:
    """A fingerprint, from a fingerprint or an exact device name.

    Exact only. `inventory.find` accepts a unique prefix, which is a fine convenience for
    `fleet ssh` and a poor one for a command that edits who can reach what.
    """
    if name_or_fp in acc.keys:
        return name_or_fp
    hits = [fp for fp, m in acc.keys.items() if m.get("name") == name_or_fp]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise AccessError(f"no machine called {name_or_fp!r} in the access list")
    raise AccessError(f"{name_or_fp!r} is ambiguous")


# --------------------------------------------------------------------- am I the center

def is_center(acc: Access | None = None, *, key_path: Path | None = None) -> bool:
    """True when this machine holds the private half of the center's key.

    Deliberately *not* a comparison against `local_device_id()`. That shells out to
    `ioreg`, which is not on cron's PATH, so a macOS center -- the nominated case -- would
    silently stop being the center under any scheduled run; it is absent on Windows; it
    is lru_cached across a handover; and `load_config` swallows YAML errors, so a stray
    tab would demote the center too. None of that buys any security: it compares one
    local file against another.

    Holding the key is both necessary and sufficient, because a machine that cannot sign
    cannot produce a list any other machine will accept.
    """
    key_path = key_path or FLEET_KEY
    pub = key_path.with_suffix(".pub")
    if not key_path.exists() or not pub.exists():
        return False
    try:
        acc = acc if acc is not None else load()
    except AccessError:
        return False
    if not acc.center:
        return False
    try:
        return fingerprint(pub.read_text()) == acc.center
    except AccessError:
        return False


# ------------------------------------------------------------------------- signing

def sign(payload: str, key_path: Path | None = None) -> str:
    """Sign with the fleet key. SSHSIG, so it needs no dependency we do not already have.

    The center dials the spoke and runs the sync filter *there*, but a grant is a key on
    that spoke -- so any granted peer could connect and claim to be the center. ssh
    authenticates *a* peer, not *the* center. The signature is what makes the claim
    checkable, and it keeps being checkable if the transport ever changes.
    """
    key_path = key_path or FLEET_KEY
    try:
        p = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key_path), "-n", SIGN_NAMESPACE, "-"],
            input=payload, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise AccessError("ssh-keygen not found -- install OpenSSH") from exc
    except subprocess.CalledProcessError as exc:
        raise AccessError(f"could not sign: {(exc.stderr or '').strip()[:200]}") from exc
    return p.stdout


def verify(payload: str, signature: str, signer_pubkey: str) -> bool:
    """Check a signature against the key we have pinned for the center."""
    import tempfile

    if not signature.strip():
        return False
    with tempfile.TemporaryDirectory() as scratch:
        allowed = Path(scratch) / "allowed_signers"
        allowed.write_text(f"center {signer_pubkey.strip()}\n")
        sig = Path(scratch) / "payload.sig"
        sig.write_text(signature)
        try:
            subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "center",
                 "-n", SIGN_NAMESPACE, "-s", str(sig)],
                input=payload, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            return False
    return True


# ------------------------------------------------------------------ the sync envelope

PROTOCOL = 2


def seal(inventory_yaml: str, *, key_path: Path | None = None,
         telemetry: list | None = None) -> str:
    """Wrap an inventory in a signature the receiver can check.

    The inventory is not incidental cargo: it holds the endpoints that decide where
    `fleet ssh oracle` actually dials. Under center-dials-spokes the sync filter runs on
    the *spoke*, and a grant is a key on that spoke, so any granted peer can reach it and
    push whatever it likes. Signing the access list and leaving this unsigned would have
    protected the policy and left the routing wide open -- and `inventory.merge` unions
    endpoints unconditionally, with no timestamp contest and no way to delete one, so an
    injected low-preference route would win and could never be removed.
    """
    key_path = key_path or FLEET_KEY
    body = yaml.safe_dump({"inventory": inventory_yaml,
                           "telemetry": telemetry or []}, sort_keys=False)
    return yaml.safe_dump({
        "protocol": PROTOCOL,
        "center_pubkey": key_path.with_suffix(".pub").read_text().strip(),
        # The signature covers the telemetry as well as the inventory. Relayed readings
        # decide where work gets sent, so an unsigned one is a way to steer a job onto a
        # machine of the sender's choosing.
        "signature": sign(body, key_path),
        "body": body,
    }, sort_keys=False)


def unseal(payload: str, signer_pubkey: str) -> str:
    """Return the inventory inside, or raise. Never returns unverified content.

    An unsigned or unsealed payload is refused outright rather than accepted as a legacy
    format: "old peer" and "hostile peer" look identical from here, and one of them must
    not be given the benefit of the doubt.
    """
    try:
        env = yaml.safe_load(payload) or {}
    except yaml.YAMLError as exc:
        raise AccessError(f"unreadable sync payload: {exc}") from exc
    return _open(env, signer_pubkey)[0]


def _open(env, signer_pubkey: str):
    """Verify and split a sealed envelope into (inventory, telemetry)."""
    if not isinstance(env, dict) or "body" not in env:
        raise AccessError("unsigned sync payload -- refusing it")
    if int(env.get("protocol") or 0) != PROTOCOL:
        raise AccessError(f"sync protocol {env.get('protocol')!r} is not {PROTOCOL}")
    body = env["body"]
    if not verify(body, env.get("signature") or "", signer_pubkey):
        raise AccessError("sync payload is not signed by the center we trust")
    inner = yaml.safe_load(body) or {}
    return inner.get("inventory", ""), list(inner.get("telemetry") or [])


def unseal_with_telemetry(payload: str, signer_pubkey: str):
    try:
        env = yaml.safe_load(payload) or {}
    except yaml.YAMLError as exc:
        raise AccessError(f"unreadable sync payload: {exc}") from exc
    return _open(env, signer_pubkey)


def trusted_center_pubkey(cache_path: Path | None = None) -> str:
    """The center's key as this machine last learned it, or "" on first contact.

    Trust on first use, then pinned -- the same bargain ssh makes with host keys, and for
    the same reason: there is no prior channel to learn it over, and refusing to start is
    not a safer outcome than recording what we saw and noticing if it changes.
    """
    path = cache_path or CACHE_PATH
    try:
        return str((yaml.safe_load(path.read_text()) or {}).get("center_pubkey") or "")
    except (OSError, yaml.YAMLError):
        return ""


def pin_center_pubkey(pubkey: str, cache_path: Path | None = None) -> None:
    path = cache_path or CACHE_PATH
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    data["center_pubkey"] = pubkey.strip()
    data["pinned_at"] = int(time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False))
    os.replace(tmp, path)


def unseal_first_contact(payload: str) -> str:
    """Accept a sealed payload from a center we have not met, and pin its key.

    Trust on first use. There is no earlier channel to learn the key over, so the choice
    is between recording what we saw and refusing to start at all -- and refusing does
    not make anyone safer, it just means the fleet cannot be set up. Every payload after
    this one is checked against what was pinned here, so an imposter has exactly one
    chance and only before the real center has ever called.
    """
    try:
        env = yaml.safe_load(payload) or {}
    except yaml.YAMLError as exc:
        raise AccessError(f"unreadable sync payload: {exc}") from exc
    if not isinstance(env, dict) or "body" not in env:
        raise AccessError("unsigned sync payload -- refusing it")
    pub = str(env.get("center_pubkey") or "")
    if not pub:
        raise AccessError("sealed payload carries no center key to pin")
    inventory, _ = _open(env, pub)
    pin_center_pubkey(pub)
    return inventory


# --------------------------------------------------------------- is the center about

# A center is a laptop. Weeks of silence are a holiday, not a revocation.
STALE_AFTER_S = 7 * 24 * 3600


def center_last_seen(cache_path: Path | None = None) -> int:
    """When the center last swept this machine. 0 if it never has."""
    path = cache_path or CACHE_PATH
    try:
        return int((yaml.safe_load(path.read_text()) or {}).get("seen_at") or 0)
    except (OSError, yaml.YAMLError, TypeError, ValueError):
        return 0


def note_center_seen(cache_path: Path | None = None) -> None:
    path = cache_path or CACHE_PATH
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    data["seen_at"] = int(time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False))
    os.replace(tmp, path)


def staleness_note(cache_path: Path | None = None) -> str:
    """A line for `ls` and `top` when the center has been quiet, or "".

    Deliberately a note and never a refusal. Everything already granted keeps working
    with the center switched off -- the keys are in authorized_keys and sshd enforces
    them without consulting fleet at all -- so treating a quiet center as a loss of
    access would turn a closed laptop into a fleet outage, which is precisely backwards.
    """
    seen = center_last_seen(cache_path)
    if not seen:
        return ""
    age = int(time.time()) - seen
    if age < STALE_AFTER_S:
        return ""
    days = age // 86400
    return (f"the center has not swept this machine for {days}d -- grants and revokes "
            "are queued until it does")


def bootstrap(name: str, pubkey: str, device_id: str = "", *,
              fleet_id: str = "", path: Path | None = None) -> Access:
    """Start a fleet, with this machine as its center. Refuses to overwrite one."""
    import uuid

    path = path or ACCESS_PATH
    if path.exists():
        raise AccessError(f"{path} already exists -- this fleet has a center already")
    fp = fingerprint(pubkey)
    acc = Access(fleet_id=fleet_id or uuid.uuid4().hex[:6], center=fp,
                 keys={fp: {"name": name, "pubkey": pubkey.strip(),
                            "device_id": device_id, "pinned_at": int(time.time())}})
    save(acc, path)
    return acc


def enroll(acc: Access, name: str, pubkey: str, device_id: str = "") -> str:
    """Pin a machine's key, learned over the center's own connection.

    Pinned rather than taken from `Device.pubkey`, which rides `inventory.merge` and can
    therefore be overwritten by a peer with a fast clock -- after which the center would
    install that peer's key wherever this machine's belonged. A changed pin is never
    accepted silently.
    """
    fp = fingerprint(pubkey)
    known = acc.keys.get(fp)
    for other, meta in list(acc.keys.items()):
        if meta.get("name") == name and other != fp:
            raise AccessError(
                f"{name} already has a different key pinned ({other[:20]}...). "
                "That is either a rebuilt machine or an impersonation; re-pin it "
                "deliberately if you know which.")
    if known is None:
        acc.keys[fp] = {"name": name, "pubkey": pubkey.strip(), "device_id": device_id,
                        "pinned_at": int(time.time())}
    return fp


# --------------------------------------------------------------------- handover

def handover_record(acc: Access, successor_fp: str) -> str:
    """The signed statement that names the next center.

    Spokes verify a list against the key they have pinned, so a new center's list is
    rejected outright unless something they already trust vouches for it. This is that
    something: signed by the outgoing center, naming the incoming one, so a machine can
    walk from whichever key it last trusted to the current one -- exactly like a
    certificate chain, and for the same reason.
    """
    meta = acc.keys.get(successor_fp) or {}
    return yaml.safe_dump({
        "kind": "fleet-handover",
        "fleet_id": acc.fleet_id,
        "from": acc.center,
        "to": successor_fp,
        "to_pubkey": meta.get("pubkey", ""),
        "at": int(time.time()),
    }, sort_keys=False)
