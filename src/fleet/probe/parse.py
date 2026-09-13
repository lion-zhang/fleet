"""Parse the payload.sh line protocol into a Snapshot.

Design notes worth keeping:
  * The "#END" sentinel is mandatory. Without it we cannot tell a complete probe from
    one truncated by a killed ssh, so its absence is always PROBE_ERROR.
  * Every field is optional. A box with no GPU, no `who`, or a busybox `df` is normal;
    the parser must never raise on missing data, only on a missing sentinel.
"""

from __future__ import annotations

import re

from ..models import Disk, Gpu, ProcClass, Process, Service, Snapshot

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
    ("mlx_lm", "llm", "mlx"),
    ("text-generation", "llm", "tgi"),
    ("llama-server", "llm", "llama.cpp"),
    ("lmstudio", "llm", "lmstudio"),
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
# Ports that commonly carry an OpenAI-compatible API; the HTTP probe confirms.
LLM_HINT_PORTS = frozenset({8000, 8001, 8010, 8080, 11434, 1234, 5000, 30000})
# Never surface these as "services" -- they are OS plumbing, not something you can use.
BORING_PORTS = frozenset({22, 53, 111, 631, 5353, 68, 67, 123})


class MissingSentinel(ValueError):
    """Raised when the payload has no '#END' line: truncated, killed, or shell died."""


def _int(v: str | None) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _float(v: str | None) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


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
    for needle, kind, framework in _SERVICE_SIGNATURES:
        if needle in low:
            return kind, framework
    if port in LLM_HINT_PORTS:
        return "llm?", ""      # unconfirmed; the HTTP probe decides
    return "unknown", ""


def parse_payload(stdout: str) -> Snapshot:
    """Parse payload.sh output. Raises MissingSentinel if the probe did not complete."""
    lines = stdout.splitlines()
    if not any(ln.startswith("#END") for ln in lines):
        raise MissingSentinel("payload produced no '#END' sentinel (truncated or killed)")

    kv: dict[str, str] = {}
    section = ""
    rows: dict[str, list[str]] = {}

    for raw in lines:
        ln = raw.rstrip()
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
        if len(f) >= 4 and _int(f[1]):   # 5th field (rw) is newer; older probes omit it
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
        comm = f[4].strip()
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

    return snap
