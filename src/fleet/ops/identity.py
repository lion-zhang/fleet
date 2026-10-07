"""Which machine this is.

Its own module because everything asks: the sweep, so the center does not ssh to itself;
the row builder, so the local machine is probed without a network; `fleet center`, to
notice it holds the role. Answering from `cli.py` meant an operation could not ask
without importing the argument parser.

Callers reach it as `identity.local_device_id()` rather than importing the name. That is
not style: the answer is cached and derived from the hardware, so a test that wants to
be some other machine has to rebind it, and a name imported into six modules is six
bindings to rebind and five chances to miss one.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from ..ssh.cmd import local_platform


def local_device_id() -> str:
    """This machine's identity, in the same shape onboard.py stamps on a probed device.

    Used to notice that we ARE a given machine -- the center, or the row `fleet show`
    describes. A machine with no machine-id at all (many containers) falls back to its
    fleet key, as joining does (`join._stable_id`): otherwise every such machine was
    `net:localhost:22`, one id for all of them, and none ever recognised its own record.
    """
    base = _machine_id() or _key_id()
    return assigned_id(base) or base


def device_id_file() -> Path:
    """Where fleet keeps the id it gave this machine because it is a clone of another."""
    from .. import config

    return config.CONFIG_DIR / "device-id"


def assigned_id(base: str) -> str:
    """The id written for this machine as a clone, if it still describes this machine.

    Only an id that extends `base` -- the one derived from this machine's own
    machine-id -- counts. A file carried over into an image whose machine-id was then
    regenerated describes the machine it was copied from, not this one.
    """
    try:
        text = device_id_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if base and text.startswith(base + ":") else ""


def adopt_id(device_id: str) -> None:
    """Record the id the fleet knows this machine by, when it differs from the derived
    one (see `assigned_id`). Removes the file when they agree again."""
    from ..state.writes import atomic_write

    base = _machine_id() or _key_id()
    path = device_id_file()
    if device_id and device_id.startswith(base + ":"):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, device_id + "\n", mode=0o644)
    elif device_id == base and path.exists():
        path.unlink()


def _key_id() -> str:
    from .. import config
    from ..state import access as acl

    try:
        pub = config.FLEET_KEY.with_suffix(".pub").read_text(encoding="utf-8")
        return f"key:{acl.fingerprint(pub)}"
    except (OSError, acl.AccessError):
        return ""


@lru_cache(maxsize=1)
def _machine_id() -> str:
    if local_platform() == "windows":
        # The same registry value payload.ps1 reads, so this agrees with the id
        # `derive_id` stamps from a probe -- which is the whole point: without it a
        # Windows center did not recognise its own row, tried to ssh to itself, and
        # reported "device has no endpoints" about the machine it was running on.
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as key:
                guid = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
            if guid:
                # `linux:machine-id:` is what derive_id stamps for anything not macOS.
                # Misleading on Windows, but the two must agree, and they are opaque.
                return f"linux:machine-id:{guid}"
        except (ImportError, OSError):
            pass
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return f"linux:machine-id:{value}"
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                             capture_output=True, text=True, timeout=5)
        found = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out.stdout)
        if found:
            return f"darwin:hwuuid:{found.group(1)}"
    except (OSError, subprocess.SubprocessError):
        pass
    return ""
