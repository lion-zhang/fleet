"""Turn a pasted ssh command into an inventory record: resolve, connect, fingerprint,
classify, name, dedupe."""

from __future__ import annotations

import re
import unicodedata

from .models import Device, Kind, ProbeResult, Snapshot, Status
from .probe.runner import run_probe, run_probe_local
from .ssh.auth import classify, probe_server
from .ssh.cmd import Endpoint, classify_route, parse_ssh_command, resolve

_RENTAL_HOST_RE = re.compile(r"(vast\.ai|runpod|autodl|seetacloud|lambdalabs|paperspace)", re.I)


def derive_id(snap: Snapshot | None, ep: Endpoint) -> str:
    """Stable identity, preferred over any address.

    Addresses lie: rentals recycle IPs and ports, and one box answers on several names.
    machine-id does not, which is what makes dedupe possible at all.
    """
    if snap and snap.machine_id:
        prefix = "darwin:hwuuid" if snap.os.lower().startswith("macos") else "linux:machine-id"
        return f"{prefix}:{snap.machine_id}"
    return f"net:{ep.target}:{ep.port}"


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "device"


def _is_address(value: str) -> bool:
    """An IPv4 address, or something close enough that trimming it would be wrong."""
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def suggest_name(snap: Snapshot | None, ep: Endpoint, taken: set[str]) -> str:
    for cand in ((snap.vast_label if snap else ""), (snap.hostname if snap else ""), ep.target):
        # Trim an FQDN to its first label *before* slugifying. Doing it afterwards can
        # never work: slugify turns every dot into a dash, so there is nothing left to
        # split on -- which is why a tailnet host came out as
        # "box-tailXXXXXX-ts-net" rather than "box". An address is left whole, since
        # the first label of 1.2.3.4 is not a name.
        head = cand.split(".", 1)[0] if cand and not _is_address(cand) else cand
        base = slugify(head)
        if not base or base == "device":
            continue
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        return name
    return "device"


def classify_kind(snap: Snapshot | None, ep: Endpoint, *, override: str | None = None) -> Kind:
    if override:
        return Kind(override)
    if snap:
        if snap.vast_label or snap.runpod_id or snap.autodl:
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

    The center is never an SSH target, so it cannot add itself the way it adds everything
    else -- `fleet add "ssh localhost"` needs inbound sshd on a laptop, which is exactly
    what `run_probe_local` exists to avoid. But the center must be in its own inventory:
    it is what `is_self` marks, what the handover names, and what the access list treats
    as holding an edge to everything.

    The record deliberately has no endpoint. An address for a machine you are already on
    is not useful, and inventing one would publish a route into the fleet that the design
    says must not exist.
    """
    res = run_probe_local(timeout=timeout)
    snap = res.snapshot
    ep = Endpoint(target="localhost", user="", port=22)
    dev = Device(
        id=derive_id(snap, ep),
        name=name or suggest_name(snap, ep, taken_names or set()),
        kind=classify_kind(snap, ep, override=kind),
        alias=alias,
        endpoints=[],
        needs_review=not res.ok,
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
    # Only ask who authorizes here when the key was actually refused: that is the one
    # answer that changes what we do next, and it costs an extra handshake.
    ssh_auth = classify(probe_server(ep)) if res.status is Status.AUTH_FAILED else ""
    snap = res.snapshot
    taken = taken_names or set()

    dev = Device(
        id=derive_id(snap, ep),
        name=name or suggest_name(snap, ep, taken),
        kind=classify_kind(snap, ep, override=kind),
        alias=alias,
        endpoints=[endpoint_dict(ep, via=ep.via)],
        ssh_auth=ssh_auth,
        needs_review=not res.ok,
    )
    if dev.kind is Kind.SHARED:
        # Never probe a multi-user cluster on the hot path: it is impolite, and koa04
        # in particular hangs for ~75s without VPN, which would stall every `fleet ls`.
        dev.probe_policy = "on_demand"
    if dev.kind is Kind.MOBILE:
        dev.probe_policy = "never"
    if snap and snap.vast_label:
        dev.provider = "vastai"
    return dev, res
