"""Turn a pasted ssh command into an inventory record: resolve, connect, fingerprint,
classify, name, dedupe."""

from __future__ import annotations

import re
import unicodedata

from .models import Device, Kind, ProbeResult, Snapshot, Status
from .probe.runner import run_probe, run_probe_local
from .ssh.cmd import Endpoint, classify_route, parse_ssh_command, resolve

_RENTAL_HOST_RE = re.compile(r"(vast\.ai|runpod|autodl|seetacloud|lambdalabs|paperspace)", re.I)


def derive_id(snap: Snapshot | None, ep: Endpoint) -> str:
    """Stable identity, preferred over any address.

    Addresses lie: rentals recycle IPs and ports, and one box answers on several names.
    machine-id does not, which is what makes dedupe possible at all.
    """
    if snap and snap.machine_id:
        # By `uname -s` where the probe sent it: `os` says "Mac OS X" on macOS 10.15 and
        # older, which read as Linux here while the Mac itself said darwin.
        mac = (snap.uname_s.lower() == "darwin" if snap.uname_s
               else snap.os.lower().startswith(("macos", "mac os")))
        prefix = "darwin:hwuuid" if mac else "linux:machine-id"
        base = f"{prefix}:{snap.machine_id}"
        # A clone of another machine carries the id fleet gave it (see
        # identity.assigned_id), and only one that extends its own machine-id counts.
        if snap.device_id.startswith(base + ":"):
            return snap.device_id
        return base
    return f"net:{ep.target}:{ep.port}"


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "device"


def _is_address(value: str) -> bool:
    """An IPv4 address, or something close enough that trimming it would be wrong."""
    parts = value.split(".")
    return (len(parts) == 4 and all(p.isdigit() for p in parts)) or ":" in value


def _name_candidates(snap: Snapshot | None, ep: Endpoint):
    """What a machine could be called, best first, each with where it came from.

    The name you typed to reach it comes before the name it gives itself. A cloud VM's
    hostname is whatever the image set -- `vm-0-8-ubuntu`, or the name of whoever built
    the image -- while `oracle.tailXXXXXX.ts.net` is a name someone chose; ranking the
    hostname first made `fleet add "ssh root@oracle..."` record a machine called `lion`.
    Not for a rental's shared ssh gateway (`ssh2.vast.ai` names the gateway, not the
    box), for localhost, or for an address, which is the last resort.
    """
    if snap and snap.vast_label:
        yield snap.vast_label, "its vast.ai label"
    target = ep.target or ""
    if (target and not _is_address(target) and any(c.isalpha() for c in target)
            and target.lower() not in ("localhost", "localhost.localdomain")
            and not _RENTAL_HOST_RE.search(target)):
        yield target, "the address you gave"
    if snap and snap.hostname:
        yield snap.hostname, "its hostname"
    yield target, "the address you gave"


def _first_label(cand: str) -> str:
    # Trim an FQDN to its first label *before* slugifying. Doing it afterwards can never
    # work: slugify turns every dot into a dash, so there is nothing left to split on --
    # which is why a tailnet host came out as "box-tailXXXXXX-ts-net" rather than "box".
    # An address is left whole, since the first label of 1.2.3.4 is not a name.
    return slugify(cand.split(".", 1)[0] if cand and not _is_address(cand) else cand)


def name_source(snap: Snapshot | None, ep: Endpoint) -> str:
    """Where `suggest_name` took its name from, to say so when a machine is added."""
    for cand, source in _name_candidates(snap, ep):
        if _first_label(cand) not in ("", "device"):
            return source
    return ""


def suggest_name(snap: Snapshot | None, ep: Endpoint, taken: set[str]) -> str:
    for cand, _ in _name_candidates(snap, ep):
        base = _first_label(cand)
        if not base or base == "device":
            continue
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        return name
    return "device"


def rental_provider(snap: Snapshot | None) -> str:
    """The rental provider a machine says it runs on, from what the probe saw there."""
    if snap is None:
        return ""
    return ("vastai" if snap.vast_label else "runpod" if snap.runpod_id
            else "autodl" if snap.autodl else "")


def classify_kind(snap: Snapshot | None, ep: Endpoint, *, override: str | None = None) -> Kind:
    if override:
        return Kind(override)
    if snap:
        if rental_provider(snap):
            return Kind.RENTAL
        # SLURM, or several distinct humans logged in, means it is not yours to fill.
        if snap.slurm or (snap.users or 0) > 3:
            return Kind.SHARED
    if _RENTAL_HOST_RE.search(ep.target):
        return Kind.RENTAL
    return Kind.PERMANENT


def endpoint_dict(ep: Endpoint, *, name: str = "primary", preference: int = 10,
                  via: str = "") -> dict:
    d = {"name": name, "target": ep.target, "user": ep.user, "port": ep.port,
         "preference": preference}
    if ep.identity:
        d["identity"] = ep.identity
    if ep.jump:
        d["jump"] = ep.jump
    if via or ep.via:
        d["via"] = via or ep.via
    return d


def onboard_self(*, name: str | None = None, kind: str | None = None,
                 alias: str = "", taken_names: set[str] | None = None,
                 timeout: float = 20.0) -> tuple[Device, ProbeResult]:
    """Record the machine fleet is running on, without going through SSH.

    `fleet add "ssh localhost"` would need inbound sshd on a laptop, which is exactly what
    `run_probe_local` exists to avoid. The record deliberately has no endpoint: an address
    for the machine you are already on is not useful.
    """
    import socket

    from .ops import identity

    res = run_probe_local(timeout=timeout)
    snap = res.snapshot
    ep = Endpoint(target="localhost", user="", port=22)
    # Without a reading -- a first probe slower than its timeout, as on a fresh Windows
    # runner -- derive_id falls back to `net:localhost:22`, the id that means "never
    # probed". For the machine we are on that is never necessary: its id is read from
    # the machine itself, and its name from its hostname. Recorded as `localhost`, it was
    # never recognised as this machine again.
    dev_id = derive_id(snap, ep)
    if dev_id.startswith("net:") or (snap and not snap.machine_id):
        dev_id = identity.local_device_id() or dev_id
    named_by = ep if snap and snap.hostname else Endpoint(
        target=socket.gethostname() or "localhost", user="", port=22)
    dev = Device(
        id=dev_id,
        name=name or suggest_name(snap, named_by, taken_names or set()),
        kind=classify_kind(snap, ep, override=kind),
        alias=alias,
        endpoints=[],
        needs_review=not res.ok,
        provider=rental_provider(snap),
    )
    return dev, res



def _resolved_route(target: str) -> str:
    """Classify a hostname by what it resolves to, once, at add time.

    Most targets are names, not literals, so without this almost nothing would be
    classified. Kept out of `classify_route` so that function stays pure and the DNS
    call happens only where a probe is already about to run.
    """
    import socket

    try:
        infos = socket.getaddrinfo(target, None)
    except OSError:
        return ""
    seen = {classify_route(i[4][0]) for i in infos}
    # A name that answers with both a private and a public address is reachable from
    # outside; say the stronger thing rather than picking whichever came back first.
    for kind in ("public", "mesh", "lan"):
        if kind in seen:
            return kind
    return ""


def onboard(ssh_command: str, *, name: str | None = None, kind: str | None = None,
            alias: str = "", taken_names: set[str] | None = None, timeout: float = 20.0,
            probe: bool = True) -> tuple[Device, ProbeResult]:
    """Resolve and fingerprint a host. An unreachable host is still recorded -- with a
    reason and needs_review -- because silently dropping it is worse than listing it."""
    ep = resolve(parse_ssh_command(ssh_command))
    ep.name = "primary"
    # Classify the route once, here, where a network round trip is already being paid
    # for. `via` decides both which address `edit` is willing to overwrite (an overlay
    # name is stable; a public IP churns) and whether the machine reports `public-ip`.
    ep.via = classify_route(ep.target) or _resolved_route(ep.target)

    res = run_probe(ep, timeout=timeout) if probe else ProbeResult(status=Status.SKIPPED_POLICY)
    snap = res.snapshot
    taken = taken_names or set()

    dev = Device(
        id=derive_id(snap, ep),
        name=name or suggest_name(snap, ep, taken),
        kind=classify_kind(snap, ep, override=kind),
        alias=alias,
        endpoints=[endpoint_dict(ep, via=ep.via)],
        needs_review=not res.ok,
    )
    if dev.kind is Kind.SHARED:
        # Never probe a multi-user cluster on the hot path: it is impolite, and koa04
        # in particular hangs for ~75s without VPN, which would stall every `fleet ls`.
        dev.probe_policy = "on_demand"
    if dev.kind is Kind.MOBILE:
        dev.probe_policy = "never"
    dev.provider = rental_provider(snap)
    return dev, res
