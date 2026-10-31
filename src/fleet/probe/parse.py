"""Parse the payload.sh line protocol into a Snapshot.

Design notes worth keeping:
  * The "#END" sentinel is mandatory. Without it we cannot tell a complete probe from
    one truncated by a killed ssh, so its absence is always PROBE_ERROR.
  * Every field is optional. A box with no GPU, no `who`, or a busybox `df` is normal;
    the parser must never raise on missing data, only on a missing sentinel.
"""

from __future__ import annotations

import math

import re

from .. import notes as machine_notes
from ..models import (Container, Disk, Gpu, Nic, ProcClass, Process, PyPackage, Service,
                      Snapshot, StorePath)

# Processes that hold VRAM because a desktop session exists, not because work is running.
# Verified necessary: lin-xps idles with 439 MiB held by msedge/ptyxis/nautilus/code.
DISPLAY_COMMS = frozenset({
    "msedge", "chrome", "chromium", "chromium-browse", "brave", "firefox", "firefox-bin",
    "code", "code-insiders", "electron", "slack", "discord", "spotify", "steam", "obs",
    "ptyxis", "nautilus", "gnome-shell", "gnome-software", "mutter", "kwin", "kwin_x11",
    "kwin_wayland", "plasmashell", "Xorg", "X", "Xwayland", "sway", "Hyprland",
    "nvtop", "nvidia-settings", "compiz", "cinnamon", "xfwm4", "picom", "WindowServer",
})
SYSTEM_COMMS = frozenset({"systemd", "dbus-daemon", "gdm", "gdm3", "sddm", "lightdm"})

_SERVICE_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    # (comm substring, kind, framework)
    ("vllm", "llm", "vllm"),
    ("sglang", "llm", "sglang"),
    ("ollama", "llm", "ollama"),
    ("omlx", "llm", "omlx"),
    ("mlx_lm", "llm", "mlx"),
    ("text-generation", "llm", "tgi"),
    ("llama-server", "llm", "llama.cpp"),
    ("lmstudio", "llm", "lmstudio"),
    ("koboldcpp", "llm", "koboldcpp"),
    ("tabby", "llm", "tabbyapi"),
    ("litellm", "llm", "litellm"),
    ("local-ai", "llm", "localai"),
    ("jupyter", "notebook", "jupyter"),
    ("milvus", "vectordb", "milvus"),
    ("neo4j", "graphdb", "neo4j"),
    ("java", "graphdb", "neo4j"),
    ("qdrant", "vectordb", "qdrant"),
    ("redis", "cache", "redis"),
    ("postgres", "db", "postgres"),
    ("tensorboard", "web", "tensorboard"),
    ("ray", "compute", "ray"),
)
# Programs that sit on a port model servers also use, and are never one.
_NOT_MODEL_SERVERS = ("controlce", "ctrlcenter", "airplay")
# Ports that commonly carry an OpenAI-compatible API; the HTTP probe confirms.
LLM_HINT_PORTS = frozenset({8000, 8001, 8010, 8080, 11434, 1234, 5000, 30000})
# Never surface these as "services" -- they are OS plumbing, not something you can use.
BORING_PORTS = frozenset({22, 53, 111, 631, 5353, 68, 67, 123})


# What a command line must never carry out of the machine: a job started with
# `--hf-token hf_...` would otherwise show its token to every agent that lists jobs.
# A value is a word, or a quoted string whole: `--token "a b"` left `b"` showing.
_VALUE = r"""("[^"]*"?|'[^']*'?|\S+)"""
_SECRET_FLAG = re.compile(
    r"(?i)(--?(?:[\w-]*(?:token|secret|passw(?:or)?d|api[-_]?key|access[-_]?key|auth)[\w-]*"
    r"|(?:[\w-]*[-_])?key"                   # --key, --hf-key; not --keyframes
    r"|(?:[\w-]*[-_])?pass|requirepass|[\w-]*passphrase[\w-]*)"   # not --passes
    r"(?:=|\s+))" + _VALUE)
_SECRET_ENV = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY)"
                         r"[A-Z0-9_]*=)" + _VALUE)
# The password in a URL: postgres://me:hunter2@db/...
_URL_PASSWORD = re.compile(r"(\b[a-z][\w+.-]*://[^\s:/@]+:)([^\s@/]+)(@)", re.IGNORECASE)
# A token sent as a header: `-H "Authorization: Bearer ..."`.
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SECRET_VALUE = re.compile(r"\b(?:hf_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}"
                           r"|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}"
                           r"|xox[abprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})")


def redact(cmdline: str) -> str:
    """The command line with every value that looks like a secret replaced by ***."""
    out = _SECRET_FLAG.sub(lambda m: m.group(1) + "***", cmdline or "")
    out = _SECRET_ENV.sub(lambda m: m.group(1) + "***", out)
    out = _URL_PASSWORD.sub(lambda m: m.group(1) + "***" + m.group(3), out)
    out = _BEARER.sub(lambda m: m.group(1) + "***", out)
    return _SECRET_VALUE.sub("***", out)


class MissingSentinel(ValueError):
    """Raised when the payload has no '#END' line: truncated, killed, or shell died."""


def _int(v: str | None) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _float(v: str | None) -> float | None:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    # `nan` and `inf` parse as floats, and a load of nan crashed `fleet top` in round()
    # and came out of `show --json` as a bare NaN that strict JSON readers refuse.
    return f if math.isfinite(f) else None


def parse_etime(v: str) -> int:
    """Accept both Linux `etimes` (plain seconds) and macOS `etime` (DD-HH:MM:SS)."""
    v = (v or "").strip()
    if not v:
        return 0
    if v.isdigit():
        return int(v)
    days = 0
    if "-" in v:
        d, _, v = v.partition("-")
        days = int(d) if d.isdigit() else 0
    parts = [int(p) if p.isdigit() else 0 for p in v.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + s


def classify(comm: str, vram_mib: int) -> ProcClass:
    base = (comm or "").strip().split("/")[-1]
    if base in DISPLAY_COMMS:
        return ProcClass.DISPLAY
    if base in SYSTEM_COMMS and vram_mib < 64:
        return ProcClass.SYSTEM
    return ProcClass.COMPUTE


def _classify_service(comm: str, port: int) -> tuple[str, str]:
    low = (comm or "").lower()
    # vLLM and SGLang name their helper processes `VLLM::EngineCore`, `VLLM::Worker_TP0`
    # and so on. Their ports are the engine's own sockets, not an API: one `vllm serve`
    # was listed as seven model servers. Only the API server (`vllm`, or `VLLM::APIServer`
    # when it runs several) serves models.
    # macOS's AirPlay receiver (ControlCenter) holds :5000 and :7000 and answers 403: it
    # was listed as a model server that "wants an API key".
    if low.startswith(_NOT_MODEL_SERVERS):
        return "unknown", ""
    for engine in ("vllm", "sglang"):
        if low.startswith(engine + "::") and not low[len(engine) + 2:].startswith(("api", "http")):
            return "internal", engine
    for needle, kind, framework in _SERVICE_SIGNATURES:
        if needle in low:
            return kind, framework
    if port in LLM_HINT_PORTS:
        return "llm?", ""      # unconfirmed; the HTTP probe decides
    return "unknown", ""


_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


# What other users run on a shared host -- a process's args, a container's name, a model
# a server lists -- reaches fleet's terminal: an escape sequence in it must not.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def parse_payload(stdout: str) -> Snapshot:
    """Parse payload.sh output. Raises MissingSentinel if the probe did not complete."""
    lines = stdout.splitlines()
    if not any(ln.startswith("#END") for ln in lines):
        raise MissingSentinel("payload produced no '#END' sentinel (truncated or killed)")

    kv: dict[str, str] = {}
    section = ""
    rows: dict[str, list[str]] = {}

    for raw in lines:
        ln = _CONTROL.sub("", raw).rstrip()
        if not ln:
            continue
        if ln.startswith("#"):
            if ln.startswith("#END"):
                break
            if ln.startswith("#FLEET"):
                continue      # version banner, not a section -- must not capture the kv block
            # "#GPU idx|uuid|..." -> section name is the first token
            section = ln[1:].split(" ", 1)[0].split("|", 1)[0].strip()
            rows.setdefault(section, [])
            continue
        if section:
            rows[section].append(ln)
        elif "=" in ln:
            k, _, v = ln.partition("=")
            kv[k.strip()] = v.strip()

    snap = Snapshot(
        hostname=kv.get("host.hostname", ""),
        machine_id=kv.get("host.machine_id", ""),
        device_id=kv.get("host.device_id", ""),
        os=kv.get("host.os", ""),
        uname_s=kv.get("host.uname_s", ""),
        kernel=kv.get("host.kernel", ""),
        arch=kv.get("host.arch", ""),
        uptime_s=_int(kv.get("host.uptime_s")),
        users=_int(kv.get("host.users")),
        is_container=kv.get("host.is_container") == "1",
        vast_label=kv.get("host.vast_label", ""),
        runpod_id=kv.get("host.runpod_id", ""),
        autodl=kv.get("host.autodl") == "1",
        slurm=kv.get("host.slurm") == "1",
        cpu_cores=_int(kv.get("cpu.cores")),
        cpu_model=kv.get("cpu.model", ""),
        mem_total_kb=_int(kv.get("mem.total_kb")),
        mem_avail_kb=_int(kv.get("mem.avail_kb")),
        gpu_present=kv.get("gpu.present", "0"),
        gpu_vendor=kv.get("gpu.vendor", ""),
        gpu_driver=kv.get("gpu.driver", ""),
        gpu_error=kv.get("gpu.error", ""),
    )

    load_parts = (kv.get("cpu.load") or "").split()
    if len(load_parts) >= 3:
        vals = [_float(p) for p in load_parts[:3]]
        if all(v is not None for v in vals):
            snap.load = (vals[0], vals[1], vals[2])  # type: ignore[assignment]

    for ln in rows.get("DISK", []):
        f = ln.split("|")
        if len(f) > 4 and f[4].strip() == "missing":
            snap.missing_disk_paths.append(f[0])
        elif len(f) >= 4 and _int(f[1]):   # 5th field (rw) is newer; older probes omit it
            snap.disks.append(Disk(f[0], _int(f[1]) or 0, _int(f[2]) or 0, _int(f[3]) or 0,
                                   writable=(f[4].strip() != "0") if len(f) > 4 else True))

    by_uuid: dict[str, Gpu] = {}
    for ln in rows.get("GPU", []):
        f = [x.strip() for x in ln.split("|")]
        if len(f) < 7:
            continue
        gpu = Gpu(
            idx=_int(f[0]) or 0, uuid=f[1], name=f[2],
            vram_total_mib=_int(f[3]) or 0, vram_used_mib=_int(f[4]) or 0,
            vram_free_mib=_int(f[5]) or 0, util_pct=_int(f[6]) or 0,
            temp_c=_int(f[7]) if len(f) > 7 else None,
            power_w=_float(f[8]) if len(f) > 8 else None,
            shared_memory=_int(f[3]) is None and "n/a" in f[3].lower(),
        )
        snap.gpus.append(gpu)
        by_uuid[gpu.uuid] = gpu

    for ln in rows.get("GPUPROC", []):
        f = ln.split("|")
        if len(f) < 6:
            continue
        vram = _int(f[2]) or 0
        comm = f[5].strip()
        klass = classify(comm, vram)
        snap.processes.append(Process(
            pid=_int(f[1]) or 0, user=f[3].strip(), comm=comm, klass=klass,
            scope="gpu", gpu_uuid=f[0].strip(), vram_mib=vram, etimes=parse_etime(f[4]),
            # rejoined: a command line may itself have held a `|` the probe removed
            args=redact("|".join(f[6:]).strip()) if len(f) > 6 else "",
        ))
        if (gpu := by_uuid.get(f[0].strip())) is not None:
            if klass is ProcClass.COMPUTE:
                gpu.compute_used_mib += vram
            else:
                gpu.display_used_mib += vram

    for ln in rows.get("CPUPROC", []):
        f = ln.split("|")
        if len(f) < 6:
            continue
        comm = f[5].strip()
        snap.processes.append(Process(
            pid=_int(f[0]) or 0, user=f[1].strip(), comm=comm,
            klass=classify(comm, 0), scope="cpu",
            cpu_pct=_float(f[2]) or 0.0, rss_kb=_int(f[3]) or 0, etimes=parse_etime(f[4]),
        ))

    for ln in rows.get("LISTEN", []):
        f = ln.split("|")
        if len(f) < 5:
            continue
        port = _int(f[2])
        if port is None or port in BORING_PORTS:
            continue
        addr = f[1].strip()
        # ss appends an interface scope ("127.0.0.53%lo"); strip it for a usable address
        addr = addr.split("%", 1)[0]
        # lsof writes a space in a program's name as `\x20` ("Adobe\x20Desktop\x20Service").
        comm = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), f[4].strip())
        kind, framework = _classify_service(comm, port)
        if kind == "unknown" and not comm:
            continue      # an anonymous high port is noise, not a service
        snap.services.append(Service(
            port=port, addr=addr, pid=_int(f[3]), comm=comm,
            name=comm or f"port-{port}", kind=kind, framework=framework,
        ))

    for ln in rows.get("SLURM", []):
        k, _, v = ln.partition("|")
        snap.slurm_info.setdefault(k.strip(), []).append(v.strip())

    for ln in rows.get("NIC", []):
        f = [x.strip() for x in ln.split("|")] + ["", "", ""]
        if not (mac := machine_notes.normalise_mac(f[1])):
            continue
        # 169.254 is a card that found no network, not a subnet to broadcast on.
        ipv4 = f[2] if _IPV4.match(f[2]) and not f[2].startswith("169.254.") else ""
        snap.nics.append(Nic(name=f[0], mac=mac, ipv4=ipv4,
                             prefix=_int(f[3]) if ipv4 else None,
                             wol=True if f[4] == "1" else False if f[4] == "0" else None))
    for ln in rows.get("GATEWAY", [])[:1]:
        f = ln.split("|")
        snap.gateway_mac = (machine_notes.normalise_mac(f[1]) if len(f) > 1 else None) or ""

    _parse_resources(snap, kv, rows)
    # Its clock, read as the probe started: a rental or a VM whose clock is hours out
    # said its software was read hours from now. Within two minutes it is the probe's
    # own run time, not the clock.
    remote_now = _int(kv.get("probe.now"))
    if remote_now and abs(snap.ts - remote_now) > 120:
        snap.clock_offset_s = snap.ts - remote_now
    return snap


def _parse_resources(snap: Snapshot, kv: dict[str, str], rows: dict[str, list[str]]) -> None:
    """The machine's own notes, its folders, its software and what its servers serve."""
    snap.light = kv.get("probe.light") == "1"
    snap.notes, snap.notes_error = machine_notes.from_probe(kv, snap.notes_skipped)

    for ln in rows.get("STORE", []):
        f = ln.split("|")
        if len(f) < 3 or not f[1].strip():
            continue
        snap.storage.append(StorePath(
            # Lowercased as the notes' own kinds are: `Models:` written by hand was
            # missing from `fleet models` and from `--tag`-free lookups by kind.
            kind=f[0].strip().lower(), path=f[1].strip(), exists=f[2].strip() == "1",
            avail_kb=_int(f[3]) if len(f) > 3 else None,
            size_kb=_int(f[4]) if len(f) > 4 else None,
            sized_at=_int(f[5]) if len(f) > 5 else None))

    for ln in rows.get("SOFTWARE", []):
        k, _, v = ln.partition("|")
        if k.strip() == "scanned_at":
            snap.software_at = _int(v)
        elif k.strip() and v.strip():
            snap.software[k.strip()] = v.strip()

    for ln in rows.get("PYENV", []):
        f = [x.strip() for x in ln.split("|")]
        if len(f) >= 4 and f[2]:
            snap.pyenvs.append(PyPackage(env=f[0], python=f[1], package=f[2], version=f[3]))

    if "CONTAINER" in rows:
        snap.containers = []
        for ln in rows["CONTAINER"]:
            f = ln.split("|")
            if len(f) >= 3:
                snap.containers.append(Container(
                    image=f[0].strip(), name=f[1].strip(), status=f[2].strip(),
                    ports="|".join(f[3:]).strip()))
        counts = dict(ln.split("|", 1) for ln in rows.get("COUNT", []) if "|" in ln)
        snap.containers_total = max(_int(counts.get("containers")) or 0, len(snap.containers))

    # Servers the machine's notes declare: marked as such, and listed even when no
    # listening port was seen for them (it may be down -- the answer below says so).
    by_port = {s.port: s for s in snap.services}
    # Where each port listens, the anonymous ones too (another user's, or Docker's proxy):
    # one on every interface was listed as 127.0.0.1, "local only", with a tunnel to dig.
    listening = {}
    for ln in rows.get("LISTEN", []):
        f = ln.split("|")
        if len(f) >= 3 and (p := _int(f[2])) is not None:
            listening.setdefault(p, f[1].strip().split("%", 1)[0])
    for name, ep in (snap.notes.get("endpoints") or {}).items():
        svc = by_port.get(ep["port"])
        if svc is None:
            svc = Service(port=ep["port"], addr=listening.get(ep["port"], "127.0.0.1"), comm="")
            snap.services.append(svc)
            by_port[ep["port"]] = svc
        svc.source, svc.name = "declared", name
        if svc.kind in ("unknown", "llm?"):
            svc.kind = "llm"
        if ep.get("path"):
            svc.url = ep["path"]          # the base path; the view adds host and port

    answered: dict[int, tuple[str, int | None, list[str]]] = {}
    totals: dict[int, int] = {}
    for ln in rows.get("LLMMODEL", []):
        f = ln.split("|")
        port = _int(f[0]) if f else None
        if port is None or len(f) < 3:
            continue
        status, ms, models = answered.get(port, (f[1].strip(), _int(f[2]), []))
        if len(f) > 3 and f[3].strip():
            models.append(f[3].strip())
        if len(f) > 4 and (n := _int(f[4])) is not None:
            totals[port] = n
        answered[port] = (status, ms, models)
    for svc in snap.services:
        if svc.port not in answered:
            continue
        svc.status, svc.latency_ms, svc.model_ids = answered[svc.port]
        svc.models_total = max(totals.get(svc.port, 0), len(svc.model_ids))
        svc.healthy = svc.status in ("ok", "auth")
        if svc.kind == "llm?" and svc.model_ids:
            svc.kind = "llm"         # it listed models: no longer a guess. A 401 alone
                                     # could be any web app with a login, so stays one.
