"""Who authorizes SSH on a host -- a capability, not a vendor.

Some overlay networks terminate SSH themselves and authorize from their own policy, so
the host's `authorized_keys` is irrelevant and fleet must not try to manage keys there.
Others are pure networking and the host still runs an ordinary sshd. That difference is a
property of the *server*, learned from what it says about itself.

It is emphatically not a property of the address. `onboard` used to infer it from a
`.ts.net` suffix, which answers the wrong question twice over: a tailnet name can front
an ordinary sshd, and an ordinary-looking address can front a server that authorizes
upstream. The suffix describes the route.

Kept as two pure functions over a captured stderr string, with the connection that
produces that string left to the caller, so the interesting half is testable without a
network.
"""

from __future__ import annotations

import re

# Servers that authorize from their own policy rather than authorized_keys.
#
# Data, not logic. Adding one is a line here and nothing else in the codebase learns a
# vendor name -- which is the mistake `Device.tailscale` and `via: tailscale` made.
EXTERNAL_SERVERS: tuple[str, ...] = ("tailscale", "netbird")

_VERSION = re.compile(r"remote software version (?P<v>.+?)\s*$", re.MULTILINE)


def server_identity(stderr: str) -> str:
    """The remote software version ssh reported, or "" if it did not say.

    ssh prints this at `-v`; it is the server's own claim about itself. An empty string
    is a normal answer -- a host that closed the connection early never got to say.
    """
    m = _VERSION.search(stderr or "")
    return m.group("v").strip() if m else ""


def classify(version: str) -> str:
    """"external" when this server authorizes elsewhere, otherwise "keys".

    Unknown servers -- including the empty string -- are "keys", deliberately. That is
    the safe direction to be wrong in: it costs one key-install offer that was going to
    be made anyway, whereas guessing "external" would silently skip the install and
    leave a host nobody can reach.
    """
    low = (version or "").lower()
    return "external" if any(name in low for name in EXTERNAL_SERVERS) else "keys"


def probe_server(ep, *, timeout: int = 8) -> str:
    """Ask a host what it is, by reading ssh's own -v chatter. "" if it never said.

    Deliberately its own connection, and deliberately only ever called on the
    AUTH_FAILED path: the answer changes almost never, it is memoised onto the device,
    and making the ordinary sweep pay for an extra handshake per host to learn something
    static would be a poor trade.
    """
    import subprocess

    argv = ["ssh", "-v", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    argv.append("true")
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return server_identity(p.stderr)
