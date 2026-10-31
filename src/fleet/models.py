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
    MOBILE = "mobile"      # no shell (iOS/Android); listed, never measured


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
    # The card has no memory of its own to report: it shares the machine's (DGX Spark's
    # GB10), and nvidia-smi says [N/A]. Read as 0, it was "0.0G idle", a card nothing
    # fits on, on a machine with 100G free to give it.
    shared_memory: bool = False

    @property
    def unattributed_mib(self) -> int:
        """VRAM held by processes we cannot see -- common on shared boxes where
        nvidia-smi hides other users' PIDs. Never treat this as free."""
        return max(0, self.vram_used_mib - self.compute_used_mib - self.display_used_mib)

    @property
    def busy(self) -> bool:
        return (self.util_pct >= 10 or self.compute_used_mib >= 1024
                or self.unattributed_mib >= 4096)    # render.view.HIDDEN_BUSY_MIB


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
    # The command line, with anything that looks like a secret replaced (parse.redact):
    # "python train.py --lr 3e-4" says what a job is, where comm only says "python".
    args: str = ""


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
    models_total: int | None = None   # how many it serves; model_ids holds the first 20
    source: str = "discovered"  # discovered | declared
    # What asking it for its models said: ok | auth (it wants a key) | none (it answered,
    # but not as a model server) | down. Empty when it was not asked.
    status: str = ""


@dataclass(slots=True)
class Disk:
    mount: str
    total_kb: int
    used_kb: int
    avail_kb: int
    # Older snapshots have no such field and are assumed writable: treating them as
    # read-only would silently stop alerting on disks that really can fill up.
    writable: bool = True


@dataclass(slots=True)
class StorePath:
    """A folder the machine's own notes say holds something: models, datasets, media."""

    kind: str
    path: str
    exists: bool = False
    avail_kb: int | None = None
    # From `du`, which runs in the background on the machine; None until it has finished.
    size_kb: int | None = None
    sized_at: int | None = None


@dataclass(slots=True)
class PyPackage:
    """One package of interest in one Python environment."""

    env: str          # the environment's folder, `~/...` under the home folder
    python: str
    package: str
    version: str


@dataclass(slots=True)
class Container:
    image: str
    name: str
    status: str
    ports: str = ""


@dataclass(slots=True)
class Nic:
    """One network card, for `fleet wake`: read while the machine is up, used once it
    is asleep."""

    name: str
    mac: str
    ipv4: str = ""
    prefix: int | None = None
    # Armed to wake on a magic packet: None when the machine would not say (no root).
    wol: bool | None = None


@dataclass(slots=True)
class Snapshot:
    """One successful probe of one device."""

    ts: int = field(default_factory=lambda: int(time.time()))
    hostname: str = ""
    machine_id: str = ""
    # The id fleet assigned a clone (see identity.DEVICE_ID_FILE); empty almost always.
    device_id: str = ""
    os: str = ""
    # `uname -s`: Linux | Darwin | Windows. A clean token both payloads have always sent
    # and the parser used to throw away, leaving OS family to be guessed from `os` free
    # text ("Ubuntu 26.04 LTS", "Microsoft Windows 11 Pro") -- which classifies a BSD or
    # OpenWrt box by elimination, i.e. wrongly. Empty on snapshots taken before this.
    uname_s: str = ""
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
    gpu_vendor: str = ""         # "nvidia" | "amd"; older probes say nothing, and mean nvidia
    gpu_driver: str = ""
    gpu_error: str = ""
    gpus: list[Gpu] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    disks: list[Disk] = field(default_factory=list)
    missing_disk_paths: list[str] = field(default_factory=list)   # watched, but not there
    slurm_info: dict[str, list[str]] = field(default_factory=dict)
    # What the machine says about itself in its own machine.yaml, checked (notes.py).
    notes: dict = field(default_factory=dict)
    notes_error: str = ""
    notes_skipped: list[str] = field(default_factory=list)   # fields of the wrong shape
    storage: list[StorePath] = field(default_factory=list)
    # cuda, nvcc, cudnn, rocm, python, uv, docker, ... -> version (or where it is)
    software: dict[str, str] = field(default_factory=dict)
    # When the software list was read on the machine (it is cached there for hours).
    software_at: int | None = None
    # How far this machine's clock is behind ours, in seconds: software_at and a folder's
    # sized_at are its clock's, and the view shows them on ours. 0 within two minutes.
    clock_offset_s: int = 0
    pyenvs: list[PyPackage] = field(default_factory=list)
    # None when the probe did not look (a light probe, or no docker), not "none running".
    containers: list[Container] | None = None
    containers_total: int | None = None   # how many run; `containers` lists the first 20
    # A quick re-read for `fleet top`: whatever the light probe skips is carried over
    # from the reading before it (store.record).
    light: bool = False
    # Its network cards, and the hardware address of its router: two homes both on
    # 192.168.1.0/24 have different routers, so this is what says two machines share a
    # LAN -- and so which one can wake the other.
    nics: list[Nic] = field(default_factory=list)
    gateway_mac: str = ""

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
    """Durable identity. Lives in machines.yaml, hand-editable, survives cache loss."""

    id: str
    name: str
    kind: Kind = Kind.PERMANENT
    # A short handle you can type instead of the name: `fleet ssh x` for `lin-xps`.
    # Optional, and unique across the fleet in the same namespace as names -- an alias
    # that collided with another machine's name would make the shorter form ambiguous
    # exactly where it is most used. Replaces `label`, which was declared, emitted in
    # --json, and never set by anything.
    alias: str = ""
    probe_policy: str = "auto"             # auto | on_demand | never
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    endpoints: list[dict] = field(default_factory=list)
    mesh: dict = field(default_factory=dict)   # optional, informational
    provider: str = ""
    cost: dict = field(default_factory=dict)
    disk_paths: list[str] = field(default_factory=list)   # empty = autodetect mounts
    # A tombstone (inventory.remove): kept a while, so a copy of the file shared with your
    # dotfiles can tell "removed" from "not added there yet".
    deleted_at: int = 0
    needs_review: bool = False
    added_at: int = field(default_factory=lambda: int(time.time()))
    # when this record last changed. added_at says when the device was first seen.
    updated_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def claimable(self) -> bool:
        """Shared boxes are never claimable: you do not own them, and a claim would be
        a lie to the other people using them."""
        return self.kind not in (Kind.SHARED, Kind.MOBILE, Kind.APPLIANCE)

    @property
    def probeable(self) -> bool:
        """Probed by a bare `fleet ls`. on_demand hosts are probed only when named
        explicitly (`fleet ls koa04 -r`), never by a bare `fleet ls`."""
        return self.probe_policy == "auto" and self.kind is not Kind.MOBILE

    @property
    def probe_mode(self) -> str:
        return "shared" if self.kind is Kind.SHARED else "full"
