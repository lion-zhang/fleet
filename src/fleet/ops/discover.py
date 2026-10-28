"""Machines you can already reach, found where you already keep them.

Two places say which machines someone uses: the `Host` entries of `~/.ssh/config`, and
the tailnet, when Tailscale is installed (`tailscale status`, no token needed). Neither
is probed until you choose to watch a machine with `fleet add NAME` -- a tailnet has
phones and printers on it, and an ssh config has hosts you have not used in years -- so
this only lists them. What is listed is the ssh command that would reach each one, and
`fleet add` takes it from there.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ..state import inventory as inv

# Tailscale's names for the systems that can run an sshd fleet can probe.
_TAILNET_OS = {"linux": "linux", "macos": "macos", "windows": "windows",
               "freebsd": "freebsd"}
# Where the CLI lives when it is not on PATH: the macOS app bundle ships it inside.
_TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


@dataclass
class Candidate:
    name: str
    target: str
    ssh_command: str
    source: str              # ssh-config | tailnet
    os: str = ""
    online: bool | None = None
    added: str = ""          # the name it is recorded under, when it already is

    def to_dict(self) -> dict:
        return asdict(self)


def _is_ip(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _config_hosts(path: Path, seen: set[Path] | None = None) -> list[str]:
    """The literal `Host` names in an ssh config, following `Include`."""
    seen = seen if seen is not None else set()
    try:
        path = path.resolve()
        if path in seen:
            return []
        seen.add(path)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, rest = line.partition(" ")
        if "=" in key:
            key, _, rest = line.partition("=")
        key, rest = key.strip().lower(), rest.strip()
        if key == "include":
            for pattern in rest.split():
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    # Relative to ~/.ssh for the user's config -- the folder it is in.
                    pattern = str(path.parent / pattern)
                for inc in sorted(glob.glob(pattern)):
                    out += _config_hosts(Path(inc), seen)
        elif key == "host":
            for h in rest.split():
                # Patterns describe many hosts and name none of them.
                if not any(c in h for c in "*?!") and h.lower() != "localhost":
                    out.append(h)
    return list(dict.fromkeys(out))


def _resolved(host: str, config: Path, timeout: float = 3.0) -> tuple[str, int] | None:
    """Where ssh would dial `host`: its HostName and Port, by `ssh -G` and this config."""
    try:
        p = subprocess.run(["ssh", "-G", "-F", str(config), "--", host], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    cfg: dict[str, str] = {}
    for line in p.stdout.splitlines():
        key, _, val = line.partition(" ")
        cfg.setdefault(key.lower(), val.strip())
    port = cfg.get("port", "22")
    return cfg.get("hostname", host).lower(), int(port) if port.isdigit() else 22


def ssh_config(home: Path | None = None) -> list[Candidate]:
    path = (home or Path.home()) / ".ssh" / "config"
    return [Candidate(name=h, target=h, ssh_command=f"ssh {h}", source="ssh-config")
            for h in _config_hosts(path)]


def _tailscale() -> str:
    found = shutil.which("tailscale")
    if found:
        return found
    return _TAILSCALE_APP if Path(_TAILSCALE_APP).exists() else ""


def tailnet_status(timeout: float = 5.0) -> dict | None:
    """`tailscale status --json`, or None when there is no Tailscale or it is down."""
    tool = _tailscale()
    if not tool:
        return None
    try:
        p = subprocess.run([tool, "status", "--json"], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def tailnet(status: dict | None = None) -> list[Candidate]:
    """Every peer that runs a system with an sshd, from `tailscale status`."""
    status = tailnet_status() if status is None else status
    if not status:
        return []
    out: list[Candidate] = []
    for peer in (status.get("Peer") or {}).values():
        os_name = _TAILNET_OS.get(str(peer.get("OS", "")).lower())
        if not os_name:
            continue                          # iOS, Android, tvOS...: nothing to ssh to
        dns = str(peer.get("DNSName") or "").rstrip(".")
        ips = peer.get("TailscaleIPs") or []
        target = dns or (ips[0] if ips else "")
        if not target:
            continue
        name = str(peer.get("HostName") or dns.split(".", 1)[0] or target).lower()
        out.append(Candidate(name=name, target=target, ssh_command=f"ssh {target}",
                             source="tailnet", os=os_name, online=bool(peer.get("Online"))))
    return sorted(out, key=lambda c: c.name)


def _hidden_path() -> Path:
    from .. import config

    return config.CONFIG_DIR / "discover-hidden.txt"


def hidden() -> set[str]:
    """Names you chose never to be offered again, one per line in fleet's config folder."""
    try:
        text = _hidden_path().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {ln.strip().lower() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}


def set_hidden(names: list[str], *, hide: bool) -> set[str]:
    """Hide or unhide names, on the list as it is now. Returns the list after."""
    from ..state.writes import atomic_write, turn
    from ..config import ensure_dirs

    ensure_dirs()
    with turn(_hidden_path()):
        now = hidden()
        now = now | {n.lower() for n in names} if hide else now - {n.lower() for n in names}
        atomic_write(_hidden_path(), "# machines `fleet ls --discover` does not offer\n"
                     + "".join(f"{n}\n" for n in sorted(now)), mode=0o644)
    return now


def candidates(*, home: Path | None = None, status: dict | None = None,
               include_hidden: bool = False) -> list[Candidate]:
    """Both sources, each machine once, marked with its name if it is already added."""
    devices = inv.live(inv.load())
    known: dict[str, str] = {}
    # Where each added machine is dialled, so a Host alias for a machine added by its
    # address (or by another alias) is not offered again as a new one.
    dialled: dict[tuple[str, int], str] = {}
    for d in devices:
        for e in d.endpoints:
            known[str(e.get("target", "")).lower()] = d.name
            if e.get("host"):
                known.setdefault(str(e["host"]).lower(), d.name)
            port = str(e.get("port") or 22)
            port = int(port) if port.isdigit() else 22
            for addr in (e.get("target"), e.get("host")):
                if addr:
                    dialled.setdefault((str(addr).lower(), port), d.name)
        known.setdefault(d.name.lower(), d.name)
    config = (home or Path.home()) / ".ssh" / "config"
    out: list[Candidate] = []
    seen: set[str] = set()
    skip = set() if include_hidden else hidden()
    for cand in ssh_config(home) + tailnet(status):
        if cand.name.lower() in skip or cand.target.lower() in skip:
            continue
        key = cand.target.lower()
        # An address's first part is not a name: "100" would make every tailnet peer
        # listed by its 100.x address after the first one look like a duplicate.
        short = key if _is_ip(key) else key.split(".", 1)[0]
        if key in seen or short in seen:
            continue
        seen.update({key, short})
        cand.added = known.get(key) or known.get(cand.name.lower()) or ""
        if not cand.added and cand.source == "ssh-config" and dialled:
            if where := _resolved(cand.target, config):
                cand.added = dialled.get(where, "")
        out.append(cand)
    return out
