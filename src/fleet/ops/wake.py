"""Wake a sleeping machine with a magic packet: `fleet wake NAME`.

A magic packet is a LAN broadcast, so it never crosses the internet or a tailnet -- and a
machine asleep is not on the tailnet anyway. It is sent from this machine when this one
is on the target's LAN, and otherwise by another fleet machine that is, over ssh, as
`fleet ssh` connects. Nothing is installed there: one python3 (or PowerShell) command.

What is known of the target was read by its last probe, while it was up: its cards'
hardware addresses, whether each is armed to wake, its subnet, and its router's hardware
address (probe/payload.sh). Its own machine.yaml can name the card instead
(`wake: {mac: ...}`). The router's address is what says two machines share a LAN: two
homes both on 192.168.1.0/24 have different routers.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import dataclass

from ..models import Device, Kind, Status
from ..notes import normalise_mac
from ..ssh.cmd import WINDOWS

__all__ = ["normalise_mac", "magic_packet", "broadcast_for", "target_macs", "same_lan",
           "Sender", "choose_senders", "relay_script", "send_local", "wakeable",
           "PORTS", "REPEATS"]

PORTS = (9, 7)            # discard and echo: what every card listens for
REPEATS = 3               # a broadcast can be dropped; three is the convention
LIMITED = "255.255.255.255"
# Kinds that are someone else's hardware: a rental is woken from the provider's console.
NEVER = (Kind.RENTAL, Kind.MOBILE)


def magic_packet(mac: str) -> bytes:
    """Six 0xFF bytes, then the card's address sixteen times: 102 bytes."""
    norm = normalise_mac(mac)
    if norm is None:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(norm.replace(":", "")) * 16


def broadcast_for(ipv4: str, prefix: int | None) -> str:
    """The subnet's own broadcast address, or the limited one when it is not known."""
    if not ipv4 or prefix is None or not 0 < prefix < 31:
        return LIMITED
    try:
        return str(ipaddress.IPv4Network(f"{ipv4}/{prefix}", strict=False).broadcast_address)
    except ValueError:
        return LIMITED


def _nics(snap: dict | None) -> list[dict]:
    return [n for n in (snap or {}).get("nics") or [] if isinstance(n, dict) and n.get("mac")]


def subnet_of(snap: dict | None) -> tuple[str, int | None]:
    """The first card with an IPv4 address: (address, prefix)."""
    for n in _nics(snap):
        if n.get("ipv4"):
            return n["ipv4"], n.get("prefix")
    return "", None


def target_macs(snap: dict | None) -> list[str]:
    """The addresses to wake: the machine's own `wake: {mac: ...}` when it names any,
    otherwise every card the probe saw that is not known to be unarmed -- those with an
    IPv4 address when any has one, since a card the machine was not using is rarely the
    one it wakes on."""
    declared = (((snap or {}).get("notes") or {}).get("wake") or {}).get("macs") or []
    if declared:
        return list(dict.fromkeys(declared))
    armed = [n for n in _nics(snap) if n.get("wol") is not False]
    used = [n for n in armed if n.get("ipv4")]
    return list(dict.fromkeys(n["mac"] for n in (used or armed)))


def _network(snap: dict | None):
    ip, prefix = subnet_of(snap)
    if not ip or prefix is None:
        return None
    try:
        return ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    except ValueError:
        return None


def same_lan(a: dict | None, b: dict | None) -> str:
    """How two machines are known to share a LAN: "gateway" (the same router), "subnet"
    (the same IPv4 subnet, said only when a router's address is missing on either side),
    or "" when they do not, or it cannot be told."""
    ga, gb = (a or {}).get("gateway_mac") or "", (b or {}).get("gateway_mac") or ""
    if ga and gb:
        return "gateway" if ga == gb else ""
    na, nb = _network(a), _network(b)
    return "subnet" if na is not None and na == nb else ""


@dataclass(slots=True)
class Sender:
    """Who sends the packet. `device` None is this machine."""

    name: str
    device: Device | None
    how: str            # local | gateway | subnet | via


def choose_senders(target: Device, target_snap: dict | None, here_snap: dict | None,
                   others: list[tuple[Device, dict | None, dict | None]], *,
                   here_name: str = "this machine", via: Device | None = None,
                   limit: int = 3) -> list[Sender]:
    """The machines to try, in order. `others` is every other machine as
    (device, last state, last snapshot); only one whose last probe answered is asked.
    The ssh to it is the real test of whether it is up: the caller moves to the next
    when it fails."""
    if via is not None:
        return [Sender(via.name, via, "via")]
    out: list[Sender] = []
    if how := same_lan(here_snap, target_snap):
        out.append(Sender(here_name, None, "local" if how == "gateway" else "subnet"))
    ranked = []
    for dev, state, snap in others:
        if dev.id == target.id or dev.kind in NEVER or not dev.endpoints:
            continue
        if (state or {}).get("status") != Status.OK.value:
            continue
        if how := same_lan(snap, target_snap):
            ranked.append((how != "gateway", dev.kind is not Kind.PERMANENT,
                           -int((state or {}).get("last_ok_at") or 0), dev.name,
                           Sender(dev.name, dev, how)))
    out += [r[-1] for r in sorted(ranked, key=lambda r: r[:4])]
    return out[:limit]


def _addresses(broadcast: str) -> list[str]:
    return list(dict.fromkeys([broadcast, LIMITED]))


def relay_script(macs: list[str], broadcast: str, *, windows: bool) -> str:
    """What the relay runs: send the packets, with nothing installed there. POSIX tries
    python3, then the `wakeonlan` tool; exit 3 when it has neither."""
    macs = [m for m in (normalise_mac(x) for x in macs) if m]
    addrs = _addresses(broadcast)
    if windows:
        mac_list = ", ".join(f"'{m}'" for m in macs)
        addr_list = ", ".join(f"'{a}'" for a in addrs)
        return (
            "$u = New-Object System.Net.Sockets.UdpClient\n"
            "$u.EnableBroadcast = $true\n"
            f"foreach ($i in 1..{REPEATS}) {{\n"
            f"  foreach ($m in @({mac_list})) {{\n"
            "    $mb = @($m -split ':' | ForEach-Object { [Convert]::ToByte($_, 16) })\n"
            "    $b = [byte[]]((,0xFF * 6) + ($mb * 16))\n"
            f"    foreach ($a in @({addr_list})) {{ foreach ($p in {', '.join(map(str, PORTS))}) "
            "{ [void]$u.Send($b, $b.Length, $a, $p) } }\n"
            "  }\n"
            "  Start-Sleep -Milliseconds 100\n"
            "}\n"
            "$u.Close()\n")
    py = ("import socket,time\n"
          # JSON lists: double quotes, inside the shell's single ones
          f"macs={json.dumps(macs)}\naddrs={json.dumps(addrs)}\n"
          "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
          "s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)\n"
          "sent=0\nerr=None\n"
          f"for _ in range({REPEATS}):\n"
          "    for m in macs:\n"
          "        pk=b\"\\xff\"*6+bytes.fromhex(m.replace(\":\",\"\"))*16\n"
          "        for a in addrs:\n"
          f"            for p in {PORTS!r}:\n"
          "                try:\n"
          "                    s.sendto(pk,(a,p)); sent+=1\n"
          "                except OSError as e:\n"
          "                    err=e\n"
          "    time.sleep(0.1)\n"
          "if not sent:\n"
          "    raise SystemExit(\"could not send: %s\" % err)\n")
    tool = "; ".join(f"wakeonlan -i {a} {m} >/dev/null" for m in macs for a in addrs)
    return (f"if command -v python3 >/dev/null 2>&1; then\n"
            f"  python3 -c '{py}'\n"
            f"elif command -v wakeonlan >/dev/null 2>&1; then\n"
            f"  {tool}\n"
            f"else\n"
            f"  echo 'neither python3 nor wakeonlan is installed here' >&2; exit 3\n"
            f"fi\n")


def send_local(macs: list[str], broadcast: str, *, sock_factory=socket.socket,
               sleep=time.sleep) -> None:
    """Send the packets from this machine. Raises OSError when not one went out."""
    # Each address on its own: macOS answers "No route to host" for 255.255.255.255 on a
    # socket not bound to a card, while the subnet's own broadcast goes out.
    sent, error = 0, None
    s = sock_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for i in range(REPEATS):
            for mac in macs:
                packet = magic_packet(mac)
                for addr in _addresses(broadcast):
                    for port in PORTS:
                        try:
                            s.sendto(packet, (addr, port))
                            sent += 1
                        except OSError as exc:
                            error = exc
            if i < REPEATS - 1:
                sleep(0.1)
    finally:
        s.close()
    if not sent:
        raise error or OSError("nothing to send")


def send(sender: Sender, macs: list[str], broadcast: str, *, endpoint=None,
         platform: str = "posix", run=None) -> tuple[bool, str]:
    """Send from `sender`: (worked, what went wrong)."""
    if sender.device is None:
        try:
            send_local(macs, broadcast)
        except OSError as exc:
            return False, str(exc)
        return True, ""
    if endpoint is None:
        return False, "no address recorded for it"
    if run is None:
        from .remote import run_script as run
    ok, said = run(endpoint, relay_script(macs, broadcast, windows=platform == WINDOWS),
                   platform=platform, timeout=30)
    return ok, "" if ok else (said or "it failed").strip().splitlines()[-1][:200]


def wakeable(dev: Device, snap: dict | None) -> bool:
    """Whether `fleet wake` has what it needs: a machine of the user's own, with a card
    to send to."""
    return dev.kind not in NEVER and bool(target_macs(snap))
