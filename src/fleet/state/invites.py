"""Invitations: the center saying in advance that one machine may join.

`fleet add` admits a machine by the center dialling it, which needs the center to reach
it and a way in -- a key it already accepts, or a password typed once. An invite turns
that round: the new machine dials the center's listener, and holding the invite is what
admits it. No password anywhere, and a machine the center cannot reach can still join.

What makes that safe enough to offer:

* **Single use.** The first machine to redeem an invite owns it; anyone after is
  refused, so a code that leaks after use is worth nothing. The one exception is the
  same key redeeming again, which is a reply lost on the way back, not a second machine.
* **Short-lived.** Minutes by default. An invite is for a machine you are setting up
  now, not a standing credential.
* **The secret never crosses the wire.** The joiner sends a MAC over its signed request,
  keyed by the invite, so someone watching the listener's plain HTTP sees nothing they
  could redeem for a key of their own.
* **Stored as a hash.** The center keeps sha256 of the secret, which is also the MAC
  key. Reading this file grants what the invite grants, for as long as it lives -- which
  is minutes, on the one machine whose compromise is total compromise anyway.

Center only, like the access list, and named so it cannot be mistaken for anything else
in a directory that is shared with the state directory on macOS.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from ..config import STATE_DIR
from .writes import atomic_write, turn

INVITES_PATH = STATE_DIR / "access-invites.yaml"     # center only

DEFAULT_TTL_S = 15 * 60
# A used or expired invite is kept a while so `fleet invite --list` can say what
# happened to it -- "used by X" is the answer to "did my machine join?" -- then dropped.
KEEP_AFTER_S = 24 * 3600


class InviteError(RuntimeError):
    """Why an invite was not honoured. Safe to show the machine that tried."""


@dataclass(slots=True)
class Invite:
    id: str
    key: str                       # sha256(secret), hex: the stored half, and the MAC key
    created_at: int
    expires_at: int
    name: str = ""                 # what the machine will be called; "" lets it say
    used_at: int = 0
    used_by: str = ""              # fingerprint of the key that redeemed it
    used_as: str = ""              # the name it joined under
    revoked_at: int = 0

    def state(self, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        if self.revoked_at:
            return "revoked"
        if self.used_at:
            return "used"
        if now >= self.expires_at:
            return "expired"
        return "open"


def secret_key(secret: str) -> str:
    """The stored half of a secret, and the key its MACs are made with."""
    return hashlib.sha256(secret.encode()).hexdigest()


def mac(secret_key_hex: str, message: str) -> str:
    """What a joiner sends instead of the secret: proof it holds it, bound to `message`."""
    return hmac.new(bytes.fromhex(secret_key_hex), message.encode(),
                    hashlib.sha256).hexdigest()


def _lock(path: Path):
    """Writes to the invite list take their turn, like every other state file."""
    return turn(path)


def _read(path: Path) -> list[Invite]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    except (OSError, yaml.YAMLError) as exc:
        # Unreadable is not "no invites": refusing every join is the safe reading, and
        # saying why is what lets someone fix it.
        raise InviteError(f"unreadable invite list at {path}: {exc}") from exc
    fields = Invite.__dataclass_fields__
    return [Invite(**{k: v for k, v in row.items() if k in fields})
            for row in (raw.get("invites") or []) if isinstance(row, dict)]


def _write(invites: list[Invite], path: Path) -> None:
    # Owner-only from its first byte: the file holds MAC keys.
    atomic_write(path, yaml.safe_dump({"invites": [asdict(i) for i in invites]},
                                      sort_keys=False))


def _prune(invites: list[Invite], now: int) -> list[Invite]:
    def done_at(i: Invite) -> int:
        return i.revoked_at or i.used_at or i.expires_at
    return [i for i in invites if i.state(now) == "open" or now - done_at(i) < KEEP_AFTER_S]


def load(path: Path | None = None) -> list[Invite]:
    return _read(path or INVITES_PATH)


def create(*, name: str = "", ttl_s: int = DEFAULT_TTL_S,
           path: Path | None = None) -> tuple[Invite, str]:
    """A new invite and its secret. The secret is returned once and never stored."""
    path = path or INVITES_PATH
    if ttl_s <= 0:
        raise InviteError("an invite has to be valid for some time")
    now = int(time.time())
    secret = secrets.token_urlsafe(24)
    invite = Invite(id=secrets.token_hex(4), key=secret_key(secret), created_at=now,
                    expires_at=now + int(ttl_s), name=name)
    with _lock(path):
        invites = _prune(_read(path), now)
        invites.append(invite)
        _write(invites, path)
    return invite, secret


def revoke(invite_id: str, *, path: Path | None = None) -> Invite:
    path = path or INVITES_PATH
    with _lock(path):
        invites = _read(path)
        for inv in invites:
            if inv.id == invite_id:
                if inv.state() == "open":
                    inv.revoked_at = int(time.time())
                    _write(invites, path)
                return inv
    raise InviteError(f"no invite {invite_id!r}")


def check(invite_id: str, message: str, offered_mac: str, fingerprint: str, *,
          path: Path | None = None, now: int | None = None) -> Invite:
    """The invite a request may redeem, or InviteError. Changes nothing.

    Split from `redeem` so the center can do everything that might still fail --
    pinning the key, recording the machine -- between deciding the invite is good and
    spending it. An invite spent on a join that then failed would be lost for nothing.
    """
    now = int(time.time()) if now is None else now
    inv = next((i for i in load(path) if i.id == invite_id), None)
    # One message for "no such invite" and "wrong secret": telling them apart would
    # confirm to a guesser which ids exist.
    if inv is None or not hmac.compare_digest(mac(inv.key, message), offered_mac or ""):
        raise InviteError("this invite is not one the center issued")
    if inv.revoked_at:
        raise InviteError("this invite was withdrawn on the center")
    if inv.used_at:
        if inv.used_by == fingerprint:
            return inv                   # the same machine again: its reply was lost
        raise InviteError("this invite has already been used by another machine")
    if now >= inv.expires_at:
        raise InviteError("this invite has expired -- ask for a new one")
    return inv


def redeem(invite_id: str, fingerprint: str, name: str, *,
           path: Path | None = None) -> Invite:
    """Spend the invite on this key. Under the lock, so two racing joins cannot both win."""
    path = path or INVITES_PATH
    with _lock(path):
        invites = _read(path)
        for inv in invites:
            if inv.id != invite_id:
                continue
            if inv.used_at and inv.used_by != fingerprint:
                raise InviteError("this invite has already been used by another machine")
            if not inv.used_at:
                inv.used_at, inv.used_by, inv.used_as = int(time.time()), fingerprint, name
                _write(invites, path)
            return inv
    raise InviteError("this invite is not one the center issued")
