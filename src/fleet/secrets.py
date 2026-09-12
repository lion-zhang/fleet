"""Reading passwords an older fleet stored, so they can be migrated away.

Nothing writes here any more. Passwords are gone: the center installs an SSH key once,
using whatever gets in that first time, and every later connection is key auth -- so
there is no longer a credential to keep, encrypt, or distribute.

What survives is the read path, and only until `fleet access --migrate` has spent the
last of them. Deleting it in the same release that added the migration would have
stranded the data behind a dependency the user can no longer install.
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


def load_identity(path: Path | None = None):
    pyrage = _pyrage()
    path = path or IDENTITY_PATH
    try:
        return pyrage.x25519.Identity.from_str((path).read_text().strip())
    except OSError as exc:
        raise SecretsError(f"no identity at {path} -- run `fleet identity`") from exc


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
