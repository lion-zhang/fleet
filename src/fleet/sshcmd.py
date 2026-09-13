"""Turn a pasted ssh command into a connectable endpoint.

The whole "just give me the ssh command" onboarding promise rests on this module, and
its central decision is to NOT hand-roll ~/.ssh/config parsing. `ssh -G` prints the
*effective* configuration after applying aliases, Match blocks, Include directives and
built-in defaults -- it is the only source that cannot disagree with the real connection.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field

from .config import FLEET_KEY

IS_WINDOWS = sys.platform == "win32"

# ssh flags that take a value; anything else single-letter is a boolean switch.
_VALUE_FLAGS = frozenset("bcDEeFIiJLlmOopQRSWw")


@dataclass(slots=True)
class ParsedSsh:
    target: str = ""                       # [user@]host as written
    user: str | None = None
    port: int | None = None
    identity: str | None = None
    jump: str | None = None
    options: list[str] = field(default_factory=list)   # raw -o k=v
    remote_command: str = ""


@dataclass(slots=True)
class Endpoint:
    """A resolved, connectable address. One device may have several."""

    target: str                            # hostname to dial
    user: str = ""
    port: int = 22
    identity: str = ""
    jump: str = ""
    name: str = "default"
    preference: int = 10
    via: str = ""                          # mesh | lan | public

    def ssh_command(self) -> str:
        bits = ["ssh"]
        if self.port and self.port != 22:
            bits += ["-p", str(self.port)]
        if self.identity:
            bits += ["-i", self.identity]
        if self.jump:
            bits += ["-J", self.jump]
        bits.append(f"{self.user}@{self.target}" if self.user else self.target)
        return " ".join(bits)


def parse_ssh_command(cmd: str) -> ParsedSsh:
    """Parse `ssh -p 58418 -i ~/.ssh/k root@1.2.3.4 [remote cmd]` or a bare alias."""
    tokens = shlex.split(cmd.strip())
    if tokens and os.path.basename(tokens[0]) in ("ssh", "ssh.exe"):
        tokens = tokens[1:]

    out = ParsedSsh()
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") and len(tok) > 1 and not out.target:
            flag, inline = tok[1], tok[2:]
            if flag in _VALUE_FLAGS:
                val = inline or (tokens[i + 1] if i + 1 < len(tokens) else "")
                if not inline:
                    i += 1
                match flag:
                    case "p":
                        out.port = int(val) if val.isdigit() else None
                    case "i":
                        out.identity = val
                    case "J":
                        out.jump = val
                    case "l":
                        out.user = val
                    case "o":
                        out.options.append(val)
            i += 1
            continue
        rest.append(tok)
        i += 1

    if rest:
        out.target = rest[0]
        out.remote_command = " ".join(rest[1:])
    if "@" in out.target:
        user, _, host = out.target.rpartition("@")
        out.user = out.user or user
        out.target = host
    return out


def resolve(parsed: ParsedSsh, *, timeout: float = 10.0) -> Endpoint:
    """Ask ssh itself what this command actually resolves to."""
    argv = ["ssh", "-G"]
    if parsed.port:
        argv += ["-p", str(parsed.port)]
    if parsed.identity:
        argv += ["-i", parsed.identity]
    if parsed.jump:
        argv += ["-J", parsed.jump]
    for opt in parsed.options:
        argv += ["-o", opt]
    argv.append(f"{parsed.user}@{parsed.target}" if parsed.user else parsed.target)

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ValueError(f"ssh -G failed for {parsed.target!r}: {proc.stderr.strip()[:200]}")

    cfg: dict[str, str] = {}
    identities: list[str] = []
    for line in proc.stdout.splitlines():
        key, _, val = line.partition(" ")
        key, val = key.strip().lower(), val.strip()
        if key == "identityfile":
            identities.append(val)          # ssh emits one line per candidate, in order
        else:
            cfg.setdefault(key, val)        # first wins, as ssh does

    # ssh lists up to five default identity files whether or not they exist; keep the
    # first that is actually present so IdentitiesOnly=yes has something to offer.
    identity = ""
    for cand in identities:
        expanded = os.path.expanduser(cand)
        if os.path.exists(expanded):
            identity = expanded
            break

    return Endpoint(
        target=cfg.get("hostname") or parsed.target,
        user=cfg.get("user", ""),
        port=int(cfg.get("port", "22") or 22),
        identity=identity,
        jump=cfg.get("proxyjump", "") if cfg.get("proxyjump", "none") != "none" else "",
    )


def resolve_command(cmd: str, *, timeout: float = 10.0) -> Endpoint:
    return resolve(parse_ssh_command(cmd), timeout=timeout)


def control_dir() -> str | None:
    """ControlMaster socket directory, or None where multiplexing is unavailable.

    Must be short: AF_UNIX paths cap at 104 bytes and '%C' plus a long state dir
    silently overflows it on macOS.
    """
    if IS_WINDOWS:
        return None      # Windows OpenSSH has no ControlMaster
    d = f"/tmp/fleet-{os.getuid()}"
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        return None
    return d


POSIX, WINDOWS = "posix", "windows"


def remote_platform(snapshot: dict | None) -> str:
    """Which shell the far side speaks, from the last probe.

    One function, one answer. Before this there were four places deciding it and they
    disagreed: `fleet ssh` read the snapshot, the reconciler compared `ssh_auth` against
    a value it never holds -- so every grant on a Windows host silently ran the POSIX
    script -- and handover and `--leave` simply assumed POSIX.

    Read from the probe rather than stored on the Device on purpose. A field would ride
    `inventory.merge`, where a peer with a fast clock could flip a host's platform and
    so choose which shell we hand it a command in. A machine nobody has probed reads
    POSIX, which is both the common case and the safe way to be wrong: a POSIX command
    on Windows fails loudly, where the reverse can appear to succeed.
    """
    os_name = str((snapshot or {}).get("os") or "")
    return WINDOWS if os_name.lower().startswith(("windows", "microsoft windows")) else POSIX


def remote_command(args: list[str], *, windows: bool = False) -> str:
    """What to hand ssh when the caller asked to run something, rather than get a shell.

    A non-interactive ssh runs with a minimal PATH -- no ~/.local/bin -- so anything
    installed by uv, pipx or cargo is invisible, fleet included. That is why
    `fleet ssh box -- fleet ls` failed with "command not found" while `fleet sync`
    worked: sync already wrapped its command this way.

    The login shell covers hosts that set PATH from a profile, and the explicit export
    covers hosts that set it from .bashrc, which a login shell does not read.

    Arguments are joined with spaces and left for the far shell to parse, exactly as
    OpenSSH itself joins them. Quoting each word instead would be safer in the abstract
    and would break `fleet ssh box "fleet ls | head"`, which is how people actually use
    it.
    """
    if not args:
        return ""                           # no command: the user wants a login shell
    if windows:
        # Every word of the reasoning above is POSIX: there is no `sh`, no login shell
        # and no ~/.local/bin. cmd.exe hands `sh -lc '...'` straight back as "not
        # recognized", so the wrapper that makes this work everywhere else is exactly
        # what breaks it here. Windows sets PATH machine-wide, so passing the command
        # through is both simpler and correct.
        return " ".join(args)
    inner = 'export PATH="$HOME/.local/bin:$PATH"; ' + " ".join(args)
    return "sh -lc " + shlex.quote(inner)



# Overlay suffixes worth knowing by name. Deliberately a table, not logic: fleet is
# neutral about which overlay you run, and adding one must not mean touching a
# classifier. Address ranges cannot do this job -- 100.64.0.0/10 is shared by several
# overlays and others use ranges the user picked.
_OVERLAY_SUFFIXES = (".ts.net", ".netbird.cloud", ".zerotier.net")


def route_of(stored: str, target: str = "") -> str:
    """The route kind for one endpoint: what was recorded, else what the address says.

    One helper because two callers need the same answer. `inventory.endpoints_of` wants
    it to decide which address `edit` may overwrite; `view._reachability` wants it for
    the `public-ip`/`mesh`/`lan` facts. Reading the raw record in one place and the
    normalised value in the other is how the facts came back empty for a whole fleet
    whose endpoints all said `tailscale`.

    Classifying the target as a fallback backfills every record written before routes
    were classified at all -- no DNS, so this stays safe on a read path: a literal
    address or a known overlay suffix answers, anything else stays "".
    """
    stored = (stored or "").strip().lower()
    if stored == "tailscale":
        # The pre-rename spelling, still on disk. Reading one of those as direct would
        # let `edit` overwrite the one address that never moves.
        return "mesh"
    return stored or classify_route(target)


def classify_route(target: str) -> str:
    """`mesh`, `lan`, `public`, or "" when we genuinely cannot tell.

    This fills in the vocabulary `Endpoint.via` has documented all along: until now the
    only assignment anywhere set "mesh", and only for one vendor's suffix.

    "" is a real answer and must stay one. `target` here is whatever ssh config resolved
    to, usually a hostname, and guessing `public` for every name would mark a NAS on a
    LAN-only DNS name as reachable from the internet -- the exact claim someone would
    act on. Resolution is the caller's job, at add time, where a probe is already
    happening; a view must never block on DNS.
    """
    import ipaddress

    host = (target or "").strip().rstrip(".").lower()
    if not host:
        return ""
    if host.endswith(_OVERLAY_SUFFIXES):
        return "mesh"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return ""
    # CGNAT is what every overlay hands out, and a home ISP behind it is not somewhere
    # you can dial either way -- "not public" is the part that matters here.
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return "mesh"
    if ip.is_private:
        return "lan"
    return "public" if ip.is_global else ""


def build_enroll_argv(ep: Endpoint, *, connect_timeout: int = 8) -> list[str]:
    """The *first* dial at a host, before it knows our key. Deliberately permissive.

    `build_argv` sets IdentitiesOnly=yes, which means ssh offers only the identities
    named on the command line -- and therefore **never offers an agent key**. That is
    right for the steady state (five default identities trip MaxAuthTries on strict
    servers) and exactly wrong here: the most common way into a fresh cloud VM is a key
    that lives only in your agent. Probing such a host with IdentitiesOnly would report
    AUTH_FAILED and send us asking for a password that does not exist.

    So this one omits it, and lets the agent and ~/.ssh/config answer. Once the fleet key
    is installed, every later connection goes back through `build_argv`.
    """
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if ep.identity:
        argv += ["-i", ep.identity]
    if FLEET_KEY.exists():
        argv += ["-i", str(FLEET_KEY)]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.jump:
        argv += ["-J", ep.jump]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    return argv


def build_argv(ep: Endpoint, *, connect_timeout: int = 8, multiplex: bool = True,
               remote: str = "sh -s", env: dict[str, str] | None = None) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        # accept-new, never 'no': takes first-contact keys but still fails loudly on a
        # CHANGED key, which is the right call for recycled rental ports.
        "-o", "StrictHostKeyChecking=accept-new",
        # NOT LogLevel=ERROR: it silences the very diagnostics the failure taxonomy
        # needs (a recycled rental port then returns rc=255 with EMPTY stderr, which is
        # unclassifiable). Keep full stderr and scrub known-benign lines in Python.
        "-o", "LogLevel=INFO",
        # ssh offers five default identities; on strict servers that trips MaxAuthTries.
        "-o", "IdentitiesOnly=yes",
    ]
    if ep.identity:
        argv += ["-i", ep.identity]
    # Offer the fleet key too, when there is one. IdentitiesOnly=yes above means ssh
    # sends only what we name here, so a host that knows the fleet key but not the
    # endpoint's identity would otherwise be unreachable.
    if FLEET_KEY.exists() and str(FLEET_KEY) != ep.identity:
        argv += ["-i", str(FLEET_KEY)]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.jump:
        argv += ["-J", ep.jump]
    if multiplex and (cdir := control_dir()):
        argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={cdir}/%C",
                 "-o", "ControlPersist=120"]
    argv.append(f"{ep.user}@{ep.target}" if ep.user else ep.target)
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    argv.append(f"{prefix} {remote}".strip())
    return argv
