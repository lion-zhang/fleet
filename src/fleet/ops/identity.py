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


@lru_cache(maxsize=1)
def local_device_id() -> str:
    """This machine's identity, in the same shape onboard.py stamps on a probed device.

    Used only to notice that we ARE the center, so `fleet sync` can be safe to run
    everywhere rather than being a command you must remember not to run in one place.
    """
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
            value = Path(candidate).read_text().strip()
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
