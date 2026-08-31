"""Core dataclasses. Deliberately plain: these are serialised by view.py and stored
either as YAML (durable identity) or SQLite rows (disposable telemetry)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import StrEnum


class Status(StrEnum):
    """Probe outcome. This is an enum, not a boolean, because an agent must never
    confuse "the box is down" with "my credentials are wrong"."""

    OK = "ok"
    AUTH_FAILED = "auth_failed"          # box is UP; creds are wrong. Do not retry.
    REFUSED = "refused"                  # sshd gone; for a rental, likely destroyed
    CLOSED = "closed"                    # port recycled to another tenant
    HOST_KEY_MISMATCH = "host_key_mismatch"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    PROBE_ERROR = "probe_error"          # connected, but no #END sentinel
    SKIPPED_POLICY = "skipped_policy"    # by design, not a failure


class Kind(StrEnum):
    PERMANENT = "permanent"
    RENTAL = "rental"
    SHARED = "shared"      # multi-user; polite probe, never claimable
    APPLIANCE = "appliance"
    MOBILE = "mobile"      # no shell (iOS/Android); presence only


class ProcClass(StrEnum):
    COMPUTE = "compute"
    DISPLAY = "display"    # desktop session holding VRAM -- NOT a job
    SYSTEM = "system"


@dataclass(slots=True)
class Gpu:
    idx: int
    uuid: str
    name: str
    vram_total_mib: int
    vram_used_mib: int
    vram_free_mib: int
    util_pct: int
    temp_c: int | None = None
    power_w: float | None = None
    compute_used_mib: int = 0
    display_used_mib: int = 0

    @property
    def unattributed_mib(self) -> int:
        """VRAM held by processes we cannot see -- common on shared boxes where
        nvidia-smi hides other users' PIDs. Never treat this as free."""
        return max(0, self.vram_used_mib - self.compute_used_mib - self.display_used_mib)

    @property
    def busy(self) -> bool:
        return self.util_pct >= 10 or self.compute_used_mib >= 1024


@dataclass(slots=True)
class Process:
    pid: int
    user: str
    comm: str
    klass: ProcClass
    scope: str = "cpu"          # "gpu" | "cpu"
    gpu_uuid: str = ""
    vram_mib: int = 0
    cpu_pct: float = 0.0
    rss_kb: int = 0
    etimes: int = 0


@dataclass(slots=True)
class Service:
    port: int
    proto: str = "tcp"
    addr: str = "*"
    pid: int | None = None
    comm: str = ""
    name: str = ""
    kind: str = "unknown"       # llm | vectordb | graphdb | notebook | web | ...
    framework: str = ""
    url: str = ""
    healthy: bool | None = None
    latency_ms: int | None = None
    model_ids: list[str] = field(default_factory=list)
    source: str = "discovered"  # discovered | declared


@dataclass(slots=True)
class Disk:
    mount: str
    total_kb: int
    used_kb: int
    avail_kb: int


@dataclass(slots=True)
class Snapshot:
    """One successful probe of one device."""

    ts: int = field(default_factory=lambda: int(time.time()))
    hostname: str = ""
    machine_id: str = ""
    os: str = ""
    kernel: str = ""
    arch: str = ""
    uptime_s: int | None = None
    users: int | None = None
    is_container: bool = False
    vast_label: str = ""
    runpod_id: str = ""
    autodl: bool = False
    slurm: bool = False
    cpu_cores: int | None = None
    cpu_model: str = ""
    load: tuple[float, float, float] | None = None
    mem_total_kb: int | None = None
    mem_avail_kb: int | None = None
    gpu_present: str = "0"       # "1" | "0" | "err"
    gpu_driver: str = ""
    gpu_error: str = ""
    gpus: list[Gpu] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    disks: list[Disk] = field(default_factory=list)
    slurm_info: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ProbeResult:
    """What the runner hands back. A failure still carries enough for an agent to act."""

    status: Status
    snapshot: Snapshot | None = None
    error_class: str = ""
    error_detail: str = ""
    endpoint_used: str = ""
    latency_ms: int = 0
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is Status.OK


@dataclass(slots=True)
class Device:
    """Durable identity. Lives in inventory.yaml, hand-editable, survives cache loss."""

    id: str
    name: str
    kind: Kind = Kind.PERMANENT
    label: str = ""
    probe_policy: str = "auto"             # auto | on_demand | never
    role: str = "none"                     # none | center | backup
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    endpoints: list[dict] = field(default_factory=list)
    declared_services: list[dict] = field(default_factory=list)
    tailscale: dict = field(default_factory=dict)
    provider: str = ""
    provider_instance_id: str = ""
    cost: dict = field(default_factory=dict)
    auth_state: str = "ok"                 # ok | needs_credentials
    needs_review: bool = False
    added_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def claimable(self) -> bool:
        """Shared boxes are never claimable: you do not own them, and a claim would be
        a lie to the other people using them."""
        return self.kind not in (Kind.SHARED, Kind.MOBILE, Kind.APPLIANCE)

    @property
    def probeable(self) -> bool:
        """Eligible for the automatic sweep. on_demand hosts are probed only when named
        explicitly (`fleet refresh koa04`), never by a bare `fleet ls`."""
        return self.probe_policy == "auto" and self.kind is not Kind.MOBILE

    @property
    def probe_mode(self) -> str:
        return "shared" if self.kind is Kind.SHARED else "full"
