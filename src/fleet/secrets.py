"""Passwords at rest, encrypted per machine.

`fleet key install` covers the common case by trading a password for key auth. A host
that genuinely refuses key auth still needs a stored credential, and that credential has
to be usable on every machine you own without a shared passphrase ever travelling
between them.

Each machine holds its own age identity. `secrets.age` is encrypted to every enrolled
machine's public recipient, so any of them can open it, none of them share a key, and
dropping a recipient and rewriting the file is a real revocation rather than a hope.

The recipient lives on the machine's own `Device` record, so it syncs and merges with
everything else instead of needing a schema of its own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import CONFIG_DIR, ensure_dirs
from .models import Device

IDENTITY_PATH = CONFIG_DIR / "identity.age"
SECRETS_PATH = CONFIG_DIR / "secrets.age"


class SecretsError(RuntimeError):
    """Anything that would otherwise fail silently and lose a credential."""


def _pyrage():
    try:
        import pyrage
    except ImportError as exc:                # pragma: no cover - depends on install extras
        raise SecretsError(
            "pyrage is missing -- reinstall fleet: uv tool install --force .") from exc
    return pyrage


def ensure_identity(path: Path | None = None) -> str:
    """This machine's age identity, created on first use. Returns its public recipient.

    Never regenerates: a new keypair would silently orphan every secret already
    encrypted to the old public key.
    """
    pyrage = _pyrage()
    path = path or IDENTITY_PATH
    if path.exists():
        return str(load_identity(path).to_public())
    ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = pyrage.x25519.Identity.generate()
    # written 0600 from the start: a private key must never exist world-readable, not
    # even for the instant between write and chmod.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(str(identity) + "\n")
    return str(identity.to_public())


def load_identity(path: Path | None = None):
    pyrage = _pyrage()
    path = path or IDENTITY_PATH
    try:
        return pyrage.x25519.Identity.from_str((path).read_text().strip())
    except OSError as exc:
        raise SecretsError(f"no identity at {path} -- run `fleet identity`") from exc


def recipients_of(devices: list[Device]) -> list[str]:
    """Every machine enrolled to read secrets, in inventory order."""
    # a removed machine must stop being able to read new secrets: that is what makes
    # `fleet rm` an actual revocation rather than a note to self.
    return [d.recipient for d in devices if d.recipient and not d.deleted_at]


def write_secrets(path: Path, data: dict[str, str], recipients: list[str]) -> None:
    """Encrypt to every recipient and replace the file.

    Refuses an empty recipient list: a file encrypted to nobody is unreadable to
    everyone including you, and writing that brick over a working secrets file would
    destroy exactly what it was meant to protect.
    """
    pyrage = _pyrage()
    if not recipients:
        raise SecretsError("no recipients -- run `fleet identity` on this machine first")
    keys = [pyrage.x25519.Recipient.from_str(r) for r in recipients]
    blob = pyrage.encrypt(json.dumps(data).encode(), keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)


def read_secrets(path: Path, identity) -> dict[str, str]:
    """Decrypt with this machine's identity. A missing file is an empty set, not an
    error -- most fleets store no passwords at all."""
    pyrage = _pyrage()
    try:
        blob = path.read_bytes()
    except OSError:
        return {}
    try:
        return json.loads(pyrage.decrypt(blob, [identity]).decode())
    except Exception as exc:
        raise SecretsError(
            f"cannot decrypt {path} with this machine's identity -- it may not be an "
            "enrolled recipient") from exc
