"""Turn a pasted ssh command into an inventory record: resolve, connect, fingerprint,
classify, name, dedupe."""

from __future__ import annotations

import re
import unicodedata

from .models import Device, Kind, ProbeResult, Snapshot, Status
from .probe.runner import run_probe
from .sshcmd import Endpoint, parse_ssh_command, resolve

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


def suggest_name(snap: Snapshot | None, ep: Endpoint, taken: set[str]) -> str:
    for cand in ((snap.vast_label if snap else ""), (snap.hostname if snap else ""), ep.target):
        base = slugify(cand)
        if not base or base == "device":
            continue
        # a tailnet FQDN is a fine name once trimmed to its first label
        base = base.split(".")[0] if not base.replace("-", "").isdigit() else base
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


def onboard(ssh_command: str, *, name: str | None = None, kind: str | None = None,
            taken_names: set[str] | None = None, timeout: float = 20.0,
            probe: bool = True) -> tuple[Device, ProbeResult]:
    """Resolve and fingerprint a host. An unreachable host is still recorded -- with a
    reason and needs_review -- because silently dropping it is worse than listing it."""
    ep = resolve(parse_ssh_command(ssh_command))
    ep.name = "primary"
    if ep.target.endswith(".ts.net"):
        ep.via = "tailscale"

    res = run_probe(ep, timeout=timeout) if probe else ProbeResult(status=Status.SKIPPED_POLICY)
    snap = res.snapshot
    taken = taken_names or set()

    dev = Device(
        id=derive_id(snap, ep),
        name=name or suggest_name(snap, ep, taken),
        kind=classify_kind(snap, ep, override=kind),
        endpoints=[endpoint_dict(ep, via=ep.via)],
        auth_state="needs_credentials" if res.status is Status.AUTH_FAILED else "ok",
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
