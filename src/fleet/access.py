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
