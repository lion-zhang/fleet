"""THE canonical serializer.

Every surface -- `fleet ls --json`, the web /api, and every MCP tool -- renders through
this module. Downstream layers may only *subset* what it returns; none may compute a
field of its own. That makes "the dashboard and the agent disagree" a test failure
rather than a bug you find at 2am.
"""

from __future__ import annotations

import re

import time
from enum import StrEnum
from typing import Any

from ..models import Device, Kind, Status
from ..ops.wake import wakeable
from ..ssh.cmd import route_of
from ..state.store import age_s
from ..state.timing import since


class Detail(StrEnum):
    COMPACT = "compact"     # list views; no process lists
    FULL = "full"           # single-device views


# Memory held by processes nvidia-smi does not show -- a job in a container, another
# user's -- past what a desktop's own windows ever take (a few hundred MiB, a GiB or two
# on Windows). Below it the card read "idle" and counted as free beside the alert saying
# not to treat it as free, and a rental training in a container was "idle -- this is
# costing money" each time its utilisation dipped.
HIDDEN_BUSY_MIB = 4096


def _gpu_view(g: dict) -> dict:
    used, compute, display = g["vram_used_mib"], g["compute_used_mib"], g["display_used_mib"]
    unattributed = max(0, used - compute - display)
    return {
        "idx": g["idx"], "name": g["name"],
        "vram_total_mib": g["vram_total_mib"],
        "vram_free_mib": g["vram_free_mib"],
        "vram_used_mib": used,
        "compute_used_mib": compute,
        "display_used_mib": display,
        "unattributed_mib": unattributed,
        "util_pct": g["util_pct"],
        "temp_c": g.get("temp_c"),
        "busy": g["util_pct"] >= 10 or compute >= 1024 or unattributed >= HIDDEN_BUSY_MIB,
        "shared_memory": bool(g.get("shared_memory")),
    }


DISK_ALERT_PCT = 90

# ---------------------------------------------------------------- derived facts
#
# Facts are what fleet can measure; `Device.tags` is what you declare. Keeping them
# apart is the point: a hand-written "gpu" tag survives the card being pulled, and the
# agent that trusted it sends a job to a machine with no GPU. Same reasoning, and the
# same wording, as auth_of above: a value with two sources that disagree is worse than
# one that is recomputed.
#
# THE INVARIANT: a fact may only be derived from a field that does not change while the
# machine exists. arch, core count, installed RAM, GPU model and VRAM qualify. Free
# memory, free disk, utilisation, load, logged-in users and container/slurm state do
# not, and neither does a rental's vast_label -- a recycled port means the cached
# snapshot is describing someone else's machine. That invariant is what lets a fact be
# computed from an old snapshot: "this box has 4 H100s" stays true while it is off.

# Rungs are the sizes real hardware ships in, not powers of two, because a 12-core part
# bucketed to 8 is a 33% lie about the number most likely to size a job.
_VRAM_GIB = (4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96, 128, 192)
_RAM_GIB = (4, 8, 16, 32, 64, 128, 256, 512, 1024)
_CORES = (2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)
_STORAGE_TIB = (1, 2, 4, 8, 16, 32, 64)

# Reported capacity runs under the nominal size, and by more than you would guess.
# Measured across seven real machines: 32 GiB laptop reports 28.5 (0.891), 4 GiB VM
# reports 3.6 (0.900), 128 GiB workstation reports 122.9 (0.960), Apple reports exactly
# 64.0. Firmware reserve and integrated-GPU carve-out account for most of it. Flooring to
# a rung instead would tag that laptop `ram-16g` -- wrong by a factor of two on the
# most-read fact in the set.
#
# 0.85 is safe rather than generous: clearing a rung falsely would need the value to
# reach 0.85x the rung ABOVE, i.e. a ladder gap under 1/0.85 = 1.18x. Every gap in every
# ladder below is at least 1.2x, and a test asserts it so a future rung cannot break it.
_SLACK = 0.85


def _ladder(value: float, rungs: tuple[int, ...], prefix: str, suffix: str) -> list[str]:
    """Every rung the value clears, smallest first -- a downward closure, not one label.

    `vram-24g` therefore means *at least* 24G, which is the only question anyone asks.
    One exact label could not answer it: `--tag` repeats as AND, so `--tag vram-24g
    --tag vram-48g` would match nothing, and an agent wanting ">= 24G" would have to know
    the whole ladder and OR across it. Closure makes the obvious query the correct one.
    """
    return [f"{prefix}{r}{suffix}" for r in rungs if value >= r * _SLACK]


def is_macos(snap: dict | None) -> bool:
    """One predicate, because `derive_id` keys device identity on the same question."""
    snap = snap or {}
    return (snap.get("uname_s") == "Darwin"
            or str(snap.get("os", "")).lower().startswith(("macos", "mac os", "darwin")))


_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
         "arm64": "arm64", "aarch64": "arm64"}


def facts(dev: Device, snap: dict | None, state: dict | None) -> list[str]:
    """What fleet can measure about this machine. Derived, never stored.

    Ordered, never a set: string hashing is randomised per process, so a set would make
    `fleet ls --json` emit different bytes on every run -- and the determinism test
    compares two calls inside one process, so it would pass while the property is broken.
    """
    out: list[str] = []
    snap = snap or {}
    gpus = snap.get("gpus") or []

    # `gpu` means you own one; `cuda` means you can run on it. They come apart when
    # nvidia-smi is present but wedged (gpu_present == "err", the classic state after a
    # kernel upgrade): the card is almost certainly there and the list is empty. Saying
    # nothing makes an expensive machine vanish from your own inventory; saying `cuda`
    # sends a job to a box that dies at torch.cuda.init.
    if gpus:
        out.append("gpu")
        # What a job needs to run on it: CUDA for NVIDIA, ROCm for AMD. An older probe
        # says no vendor, and only ever read nvidia-smi.
        out.append("rocm" if snap.get("gpu_vendor") == "amd" else "cuda")
        if len(gpus) > 1:
            out.append("multi-gpu")
        # The largest single card, never the total: a model fits in one card's VRAM or it
        # does not, and 4x24G is not a 96G machine. Same rule as free_vram_mib below.
        out += _ladder(max(g["vram_total_mib"] for g in gpus) / 1024, _VRAM_GIB, "vram-", "g")
    elif snap.get("gpu_present") == "err":
        out.append("gpu")
    elif is_macos(snap) and _ARCH.get(str(snap.get("arch", "")).lower()) == "arm64":
        # Apple Silicon. The probe runs only nvidia-smi, so a 40-core M3 Max GPU reports
        # gpu.present=0; inferring it is the only way this machine is findable at all.
        # No VRAM rung: memory is unified, and claiming a number would be a guess.
        out += ["gpu", "metal"]

    if snap:
        family = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(
            str(snap.get("uname_s", "")))
        if family is None and snap.get("os"):
            # Snapshots taken before uname_s was parsed. Only macOS and Windows are
            # named positively; everything else stays unclassified rather than being
            # called linux by elimination, which is how a BSD NAS gets mislabelled.
            low = str(snap["os"]).lower()
            family = "macos" if is_macos(snap) else "windows" if "windows" in low else None
        if family:
            out.append(family)
        if arch := _ARCH.get(str(snap.get("arch", "")).lower()):
            out.append(arch)
        if cores := snap.get("cpu_cores"):
            # Logical CPUs -- nproc, hw.ncpu -- so a 16C/32T part is cores-32. Exact, no
            # slack: a core count is a count, not a rounded capacity.
            out += [f"cores-{r}" for r in _CORES if cores >= r]
        if mem := snap.get("mem_total_kb"):
            out += _ladder(mem / 1048576, _RAM_GIB, "ram-", "g")
        if room := _biggest_volume(snap):
            out += _ladder(room, _STORAGE_TIB, "storage-", "t")

    # Projected from Device.kind rather than stored again. A derived view of one stored
    # field cannot drift out of step with it, unlike a hand-typed `rental` tag.
    if dev.kind in (Kind.RENTAL, Kind.SHARED, Kind.APPLIANCE):
        out.append(dev.kind.value)

    out += _reachability(dev, state)
    return out


def _biggest_volume(snap: dict) -> float:
    """TiB of the largest writable volume. Capacity, not free space -- free space flaps.

    used+avail, never total_kb: filesystems reserve blocks nobody can write (see
    _disk_view below). Max rather than sum, because
    bind mounts and container overlays would be counted twice.
    """
    sizes = [(d.get("used_kb", 0) + d.get("avail_kb", 0)) / 1073741824
             for d in snap.get("disks") or [] if d.get("writable", True)]
    return max(sizes, default=0.0)


def _reachability(dev: Device, state: dict | None) -> list[str]:
    """How this machine is reached, from the endpoints recorded for it."""
    vias = {route_of(e.get("via", ""), e.get("target", "")) for e in dev.endpoints}
    return [v for v in ("public-ip", "mesh", "lan")
            if (v == "public-ip" and "public" in vias) or v in vias]


# The facts that come from the list -- a machine's kind and how it is reached -- rather
# than from a reading, so they can be judged for a machine never reached.
LISTED_FACTS = frozenset({"rental", "shared", "appliance", "public-ip", "mesh", "lan"})


def usd(x: float) -> str:
    """Dollars as people read them: cents, and more digits under a cent -- a small cloud
    machine at $0.004/hr read "$0.00", as if it were free."""
    return f"${x:.2f}" if x >= 0.01 or not x else f"${x:.2g}"


def matches_tag(row: dict, tag: str) -> bool:
    """One matcher, here rather than in cli.py, so every surface agrees.

    Declared tags and derived facts share one query namespace: asking for `gpu` should
    not require knowing which of the two a given machine got it from. Fact names are
    deliberately not reserved -- a hand-set `gpu` on a box whose nvidia-smi is broken is
    the correct use of the field, and the YAML is hand-editable anyway, so a CLI-only
    rule would be one the file format ignores.
    """
    tag = normalise_tag(tag)
    return tag in row.get("tags", []) or tag in row.get("facts", [])


# The fixed half of the derived vocabulary. Used ONLY to warn when a declared tag
# shadows a fact -- never to refuse one. A hand-set `gpu` on a box whose nvidia-smi is
# wedged, or on an accelerator fleet has no probe for, is the correct use of the field.
FACT_WORDS = frozenset({
    "gpu", "cuda", "rocm", "metal", "multi-gpu",
    "linux", "macos", "windows", "x86_64", "arm64",
    "public-ip", "mesh", "lan",
    "rental", "shared", "appliance",
})
_FACT_PREFIXES = ("vram-", "ram-", "cores-", "storage-")


def is_fact_name(tag: str) -> bool:
    """Whether a name belongs to the derived vocabulary. For a warning, not a refusal."""
    tag = normalise_tag(tag)
    return tag in FACT_WORDS or tag.startswith(_FACT_PREFIXES)


def normalise_tag(tag: str) -> str:
    """Lowercase, so `--tag GPU` finds `gpu`. The request said "GPU", "NAS", "IP"."""
    return (tag or "").strip().lower()


def split_tags(values) -> list[str]:
    """Each tag in `--tag a,b` or `--tag "a b"`, normalised, once each.

    Tags are shown space-separated and written comma-separated by habit, so `--tag
    training,big` was stored as the one tag "training,big", which no `--tag training`
    ever found.
    """
    out: list[str] = []
    for v in values or []:
        for t in re.split(r"[,\s]+", str(v)):
            if (t := normalise_tag(t)) and t not in out:
                out.append(t)
    return out


def _disk_view(d: dict) -> dict:
    """One mount, in the units a human and an agent both read.

    Usage is computed against used+avail, not total: filesystems reserve blocks the
    caller can never write, and dividing by total quietly understates how full a disk
    is. On a Mac the probe reports as used what is not free: each APFS volume there
    counts only its own use against the whole disk's size.
    """
    used, avail = d.get("used_kb", 0), d.get("avail_kb", 0)
    writable = used + avail
    return {
        "mount": d.get("mount", ""),
        "total_gb": round(d.get("total_kb", 0) / 1048576, 1),
        "free_gb": round(avail / 1048576, 1),
        "use_pct": round(100 * used / writable) if writable else 0,
        "writable": bool(d.get("writable", True)),
    }


def _roomiest(snap: dict | None) -> dict | None:
    """The mount with the most room, which is the one a job should be pointed at.

    Reporting `/` would be actively misleading on a rental, where root is a small
    container overlay and the real storage is mounted somewhere else entirely.
    """
    disks = [_disk_view(d) for d in (snap or {}).get("disks", [])]
    return max(disks, key=lambda d: d["free_gb"]) if disks else None


# ---------------------------------------------------------------- resources
#
# What the machine has and is doing beyond its hardware: the jobs on its GPUs, the models
# its servers serve, its software, and the folders its own machine.yaml points at.

_LOOPBACK = ("127.", "::1", "[::1]", "localhost")


def gpu_jobs(snap: dict | None) -> dict[str, list[dict]]:
    """Each GPU's compute jobs, biggest first, keyed by the GPU's uuid."""
    out: dict[str, list[dict]] = {}
    for p in (snap or {}).get("processes", []):
        if p.get("scope") != "gpu" or p.get("klass") != "compute":
            continue
        command = p.get("args") or p.get("comm", "")
        out.setdefault(p.get("gpu_uuid", ""), []).append({
            "pid": p.get("pid"), "user": p.get("user", ""),
            # None, not 0, for a job this login cannot see into (one in a container): an
            # agent read 0 as a job that had just started.
            "vram_mib": p.get("vram_mib", 0),
            "runtime_s": p.get("etimes", 0) if p.get("user") or command else None,
            "command": command,
        })
    for jobs in out.values():
        jobs.sort(key=lambda j: -j["vram_mib"])
    return out


def _host_of(dev: Device, state: dict | None = None) -> str:
    from ..state.inventory import by_use
    eps = by_use(dev, state)
    # The address, not an ssh config alias: the URL is for an HTTP client, which has
    # never heard of ~/.ssh/config.
    return eps[0].address if eps else ""


def _url_host(host: str) -> str:
    """An IPv6 address goes in brackets in a URL: `http://2001:db8::5:8000` is no URL."""
    bare = host.strip("[]")
    return f"[{bare}]" if ":" in bare else bare


def model_servers(dev: Device, snap: dict | None, *, here: bool = False,
                  state: dict | None = None) -> list[dict]:
    """Every server here that answered as a model server, and how to reach it.

    `reach` is honest about where it listens: `local` means only from that machine, so the
    way in from anywhere else is the ssh tunnel beside it.
    """
    from ..state.inventory import by_use
    eps = by_use(dev, state)
    out = []
    for s in (snap or {}).get("services", []):
        # "none": it answered, but not with a model list. Not a model server, unless the
        # machine says it is one: a declared server at the wrong path vanished from
        # `fleet models` with no word of why.
        if not s.get("status") or s.get("status") == "none" and s.get("source") != "declared":
            continue
        if s.get("kind") not in ("llm", "llm?"):
            continue
        addr, port = str(s.get("addr") or "*"), s.get("port")
        local = addr.startswith(_LOOPBACK)
        # A declared server with nothing on its port: offering its URL or a tunnel to it
        # read as "running, but private", and an agent dug a tunnel to a closed port.
        down = s.get("status") == "down"
        # A server on IPv6 loopback alone is not at 127.0.0.1, here or down a tunnel.
        loop = "[::1]" if addr.strip("[]") == "::1" else "127.0.0.1"
        host = loop if here else _host_of(dev, state)
        base = s.get("url") if str(s.get("url") or "").startswith("/") else "/v1"
        row = {
            "port": port, "framework": s.get("framework") or "", "status": s.get("status"),
            "models": list(s.get("model_ids") or []), "latency_ms": s.get("latency_ms"),
            # "models" names at most 20; this is how many it serves
            "models_total": max(s.get("models_total") or 0, len(s.get("model_ids") or [])),
            # declared: the machine's notes name it; discovered: found by port or program
            "source": s.get("source") or "discovered",
            "name": s.get("name") if s.get("source") == "declared" else "",
            # here: the machine fleet runs on, where even a localhost-only server answers
            "reach": "down" if down else "here" if here else "local" if local else "network",
            "url": None if (local and not here) or not host
            else f"http://{_url_host(host)}:{port}{base}",
        }
        if eps and not here and not down:
            # The way fleet itself gets in, key and jump host too: without the machine's
            # own key the tunnel asked for a password a rental does not have.
            row["tunnel"] = (f"ssh -N -L {port}:{loop}:{port}"
                             + eps[0].ssh_command().removeprefix("ssh"))
        out.append(row)
    return _one_per_ollama(out)


def _one_per_ollama(rows: list[dict]) -> list[dict]:
    """The Ollama desktop app listens on a port of its own and passes the model list
    through from `ollama serve`, so every model was listed twice. Keep one: :11434,
    ollama's own port, when it is among them."""
    keep: dict[tuple, dict] = {}
    for r in sorted((r for r in rows if r["framework"] == "ollama" and r["models"]),
                    key=lambda r: (r["port"] != 11434, r["port"] or 0)):
        keep.setdefault(tuple(sorted(r["models"])), r)
    chosen = {id(r) for r in keep.values()}
    return [r for r in rows if r["framework"] != "ollama" or not r["models"]
            or id(r) in chosen]


def _version_key(v: str) -> tuple[int, ...]:
    """"2.10.0" after "2.9.1", as numbers; good enough for picking the newest of a few."""
    return tuple(int(n) for n in re.findall(r"\d+", str(v).split("+")[0])[:4])


def software_view(snap: dict | None, *, full: bool) -> dict:
    """The stack: system versions, then each Python environment and what is in it."""
    snap = snap or {}
    envs: dict[str, dict] = {}
    for p in snap.get("pyenvs", []):
        e = envs.setdefault(p.get("env", ""), {"env": p.get("env", ""),
                                               "python": p.get("python", ""), "packages": {}})
        e["packages"][p.get("package", "")] = p.get("version", "")
    if not full:
        # The one-line answer: system versions, and each package's newest version anywhere.
        best: dict[str, str] = {}
        for e in envs.values():
            for k, v in e["packages"].items():
                # The newest, as the comment says: the first environment read won, so a
                # stale venv's torch 2.1 was the answer on a box that also had 2.5.
                if k not in best or _version_key(v) > _version_key(best[k]):
                    best[k] = v
        keep = ("cuda", "nvcc", "rocm", "metal", "python", "docker")
        return {**{k: v for k, v in snap.get("software", {}).items() if k in keep}, **best}
    out = {"system": dict(snap.get("software", {})), "envs": list(envs.values())}
    if snap.get("containers") is not None:
        out["containers"] = snap["containers"]
        # `containers` lists at most 20 of them
        out["containers_total"] = max(snap.get("containers_total") or 0, len(snap["containers"]))
    return out


def storage_view(snap: dict | None, *, full: bool) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for st in (snap or {}).get("storage", []):
        row = {"path": st.get("path"), "exists": bool(st.get("exists")),
               "free_gb": round(st["avail_kb"] / 1048576, 1)
               if st.get("avail_kb") is not None else None,
               # three places under a gigabyte, or a 3 MB folder reads as empty
               "size_gb": round(st["size_kb"] / 1048576, 3 if st["size_kb"] < 1048576 else 1)
               if st.get("size_kb") is not None else None}
        if full:
            row["sized_at"] = on_our_clock(st.get("sized_at"), snap)
        out.setdefault(st.get("kind", ""), []).append(row)
    return out


def on_our_clock(at: int | None, snap: dict | None) -> int | None:
    """A time the machine stamped with its own clock, on this machine's."""
    return at + int((snap or {}).get("clock_offset_s") or 0) if at else at


def declared(dev: Device, snap: dict | None) -> tuple[list[str], dict, str]:
    """Tags, cost and notes: the inventory's, with the machine's own machine.yaml over it.

    The machine's file wins because it is the one every fleet that looks at the machine
    reads; the inventory is only what this one machine was told.
    """
    notes = (snap or {}).get("notes") or {}
    tags = [normalise_tag(t) for t in dev.tags]
    tags += [t for t in (normalise_tag(x) for x in notes.get("tags", [])) if t not in tags]
    cost = notes.get("cost") or dev.cost or {}
    return tags, cost, notes.get("notes") or dev.notes


def auth_of(dev: Device, state: dict | None) -> str:
    """Whether the machine let us in on the last try. Derived, never stored.

    "ok" until a probe says otherwise: "we have no reason to think this will fail" is the
    right default for a machine nobody has probed yet.
    """
    if state and state.get("status") == Status.AUTH_FAILED.value:
        return "refused"
    return "ok"


def _alerts(dev: Device, snap: dict | None, state: dict | None) -> list[str]:
    out: list[str] = []
    if auth_of(dev, state) == "refused":
        out.append(f"host is UP but refused us (this is not 'offline') -- "
                   f"`fleet doctor {dev.name}` says how to let us in")
    if snap:
        read = len(out)
        for g in snap.get("gpus", []):
            if (held := _gpu_view(g)["unattributed_mib"]) >= HIDDEN_BUSY_MIB:
                out.append(f"gpu{g['idx']}: {held} MiB held by "
                           "processes not visible to us -- do not treat as free")
            elif held >= 256:
                # Below what makes a card busy it is still listed idle and counted free,
                # and "do not treat as free" beside that told an agent both at once.
                out.append(f"gpu{g['idx']}: {held} MiB held by processes not visible to "
                           "us -- a desktop's windows, or a job just starting")
        for disk in snap.get("disks", []):
            dv = _disk_view(disk)
            # a read-only mount is 100% full by definition; nothing will ever die on it
            if dv["writable"] and dv["use_pct"] >= DISK_ALERT_PCT:
                out.append(f"disk {dv['mount']}: {dv['use_pct']}% full, "
                           f"{dv['free_gb']}G left -- a long job will die on this")
        for path in snap.get("missing_disk_paths") or []:
            out.append(f"disk path {path} is not there -- "
                       f"`fleet edit {dev.name} --disk-path` with the right one")
        if snap.get("gpu_present") == "err":
            if snap.get("gpu_vendor") == "amd":
                out.append(snap.get("gpu_error", "")[:120] or "an AMD GPU tool is here but read no GPU")
            else:
                out.append(f"nvidia-smi present but failing: {snap.get('gpu_error', '')[:80]}")
        if snap.get("notes_error"):
            out.append(f"{snap['notes_error'][:160]} -- its notes and paths were not read")
        for what in (snap.get("notes_skipped") or [])[:3]:
            out.append(what[:200])
        for st in snap.get("storage", []):
            if not st.get("exists"):
                out.append(f"{st.get('kind')} folder {st.get('path')} does not exist there")
        if (state or {}).get("status") not in (None, "ok"):
            # From the last reading, on a machine that did not answer this time: "disk /
            # 97% full" or "folder does not exist there" read as true now, hours later.
            out[read:] = [f"{a} (when last read)" for a in out[read:]]
    if dev.kind is Kind.SHARED:
        out.append("shared multi-user host -- not claimable")
    # Only from a reading that just came in. The snapshot outlives the machine: a rental
    # stopped on its provider's page kept saying it was idle and costing money, from its
    # last reading, while the summary rightly counted it as not running.
    if dev.kind is Kind.RENTAL and snap and (state or {}).get("status") == "ok":
        gpus = [_gpu_view(g) for g in snap.get("gpus", [])]
        if gpus and not any(g["busy"] for g in gpus):
            out.append("RENTAL is idle -- this is costing money")
    return out


def device_view(dev: Device, state: dict | None, snap: dict | None,
                detail: Detail = Detail.COMPACT, self_id: str = "") -> dict[str, Any]:
    status = (state or {}).get("status") or "unknown"
    gpus = [_gpu_view(g) for g in (snap or {}).get("gpus", [])]
    mem_total = (snap or {}).get("mem_total_kb")
    mem_avail = (snap or {}).get("mem_avail_kb")
    roomiest = _roomiest(snap)
    disks = [_disk_view(d) for d in (snap or {}).get("disks", [])]

    tags, cost, note = declared(dev, snap)
    jobs = gpu_jobs(snap)
    full = detail is Detail.FULL
    out: dict[str, Any] = {
        "name": dev.name,
        "id": dev.id,
        "alias": dev.alias,
        "kind": dev.kind.value,
        "status": status,
        "claimable": dev.claimable,
        # Requires both to be non-empty: a container with no machine-id has no identity,
        # and claiming to be some device would be worse than admitting we cannot tell.
        "is_self": bool(self_id and dev.id and dev.id == self_id),
        "telemetry_age_s": age_s(state),
        "gpus": [{"name": g["name"], "vram_total_mib": g["vram_total_mib"],
                  "vram_free_mib": g["vram_free_mib"], "util_pct": g["util_pct"],
                  "busy": g["busy"], "shared_memory": g["shared_memory"]}
                 for g in gpus] if detail is Detail.COMPACT else gpus,
        "gpu_count": len(gpus),
        "free_vram_mib": max((g["vram_free_mib"] for g in gpus), default=0),
        "ssh_auth": auth_of(dev, state),
        "cpu_cores": (snap or {}).get("cpu_cores"),
        "ram_total_gb": round(mem_total / 1048576, 1) if mem_total else None,
        "ram_free_gb": round(mem_avail / 1048576, 1) if mem_avail is not None else None,
        # the roomiest mount stays, as the one a job should be pointed at
        "disk_free_gb": roomiest["free_gb"] if roomiest else None,
        "disk_mount": roomiest["mount"] if roomiest else None,
        # ...but every mount is listed beside it, because reducing to one number hid a
        # rental's nearly full / behind its big /workspace -- and the mount that ends a
        # long job is precisely the hidden one. Trimmed to mount+free at COMPACT, the
        # way `gpus` is, so `fleet ls --json` stays small for the agents reading it.
        # A read-only mount (a disk image, a tool volume) is always "0G free" and can
        # never fill, so the short list leaves it out: it read as a full disk.
        "disks": ([{"mount": d["mount"], "free_gb": d["free_gb"]} for d in disks
                   if d["writable"]] if detail is Detail.COMPACT else disks),
        "usd_per_hour": cost.get("usd_per_hour"),
        "services": [{"name": s.get("name"), "kind": s.get("kind"), "port": s.get("port"),
                      "healthy": s.get("healthy")}
                     for s in (snap or {}).get("services", [])],
        # For choosing a machine, the names of what it serves; `show` and `fleet models`
        # carry the servers in full -- with how to reach each.
        **({"models": model_servers(dev, snap, here=bool(self_id and dev.id == self_id),
                                    state=state),
            "storage": storage_view(snap, full=True)} if full else
           {"served_models": sorted({m for s in model_servers(dev, snap)
                                     for m in s["models"]})}),
        "software": software_view(snap, full=full),
        # How old each part is: a probe reading is seconds old, a software list hours.
        "observed_at": {"probe": (snap or {}).get("ts"),
                        "software": on_our_clock((snap or {}).get("software_at"), snap)},
        "alerts": _alerts(dev, snap, state),
        # Down, and `fleet wake` has a card to send to: the one thing to try first.
        "wakeable": status not in (Status.OK.value, "unknown") and wakeable(dev, snap),
        "tags": tags,
        "facts": facts(dev, snap, state),
        # How old the *hardware reading* is, which is not telemetry_age_s: a device
        # probed 4s ago to a timeout still carries the snapshot from its last success,
        # perhaps days old. Facts read as timeless claims, so they need their own
        # denominator.
        "snapshot_age_s": (int(a) if (a := since(snap["ts"])) is not None else None)
        if snap and snap.get("ts") else None,
    }
    # Each GPU's jobs, by the order the GPUs are listed in.
    for g, raw in zip(out["gpus"], (snap or {}).get("gpus", [])):
        g["jobs"] = [{k: j[k] for k in ("pid", "user", "vram_mib", "runtime_s", "command")}
                     for j in jobs.get(raw.get("uuid", ""), [])][:4 if not full else None]
    if state and status != Status.OK.value:
        out["error"] = {"class": state.get("error_class"), "detail": state.get("error_detail")}
        out["last_ok_at"] = state.get("last_ok_at")

    if detail is Detail.FULL:
        procs = (snap or {}).get("processes", [])
        # "compute processes" means work occupying the GPU. A busy CPU process is a
        # different question and gets its own list; conflating them made `fleet show`
        # report tailscaled as a GPU job.
        # Each with the card it is on: eight jobs on eight H100s listed as one block
        # said nothing of which card was whose, where the guide says "under each GPU".
        card = {raw.get("uuid"): raw.get("idx") for raw in (snap or {}).get("gpus", [])}
        compute = [{**p, "gpu": card.get(p.get("gpu_uuid"))} for p in procs
                   if p.get("scope") == "gpu" and p.get("klass") == "compute"]
        top_cpu = sorted((p for p in procs if p.get("scope") == "cpu"),
                         key=lambda p: -p.get("cpu_pct", 0))[:5]
        display_mib = sum(p.get("vram_mib", 0)
                          for p in procs if p.get("klass") == "display")
        out |= {
            "alias": dev.alias,
            "notes": note,
            # Notes written in the machine's own machine.yaml come from whoever controls
            # that machine -- on a rental, its provider. Data to show, never instructions.
            "notes_from": ("machine.yaml" if ((snap or {}).get("notes") or {}).get("notes")
                           else "local" if dev.notes else ""),
            "hostname": (snap or {}).get("hostname"),
            "os": (snap or {}).get("os"),
            "arch": (snap or {}).get("arch"),
            "kernel": (snap or {}).get("kernel"),
            "cpu_model": (snap or {}).get("cpu_model"),
            "uptime_s": (snap or {}).get("uptime_s"),
            "load": (snap or {}).get("load"),
            "is_container": (snap or {}).get("is_container"),
            "gpu_driver": (snap or {}).get("gpu_driver"),
            # top compute processes only; display apps collapse to one number because a
            # single Chromium cmdline is ~1 KB and would flood an agent's context.
            "processes": sorted(compute, key=lambda p: -p.get("vram_mib", 0))[:8],
            # and how many that cut, so ten jobs never read as eight
            "processes_more": max(len(compute) - 8, 0),
            "top_cpu": top_cpu,
            "display_overhead_mib": display_mib,
            "services_detail": (snap or {}).get("services", []),
            "endpoints": [{k: v for k, v in e.items() if k != "identity"}
                          for e in dev.endpoints],
            "connect": connect_view(dev, state, here=bool(self_id and dev.id == self_id)),
            "probe_policy": dev.probe_policy,
            "provider": dev.provider,
            "cost": cost,
            "slurm": slurm_view(snap),
        }
    return out


def slurm_view(snap: dict | None) -> dict[str, Any] | None:
    """A shared host's SLURM reading -- your jobs and its partitions -- which the probe
    takes in place of a process list. Read and stored, then shown nowhere: `fleet show`
    on a cluster login said nothing about the one thing measured there instead."""
    info = (snap or {}).get("slurm_info") or {}
    if not info:
        return None

    def count(key: str) -> int | None:
        try:
            return int((info.get(key) or [""])[0])
        except ValueError:
            return None

    parts = []
    for line in info.get("partition") or []:
        f = str(line).split()
        if len(f) < 4:
            continue
        parts.append({"name": f[0].rstrip("*"), "default": f[0].endswith("*"),
                      "avail": f[1], "nodes": _int_or_none(f[2]), "state": f[3]})
    return {"my_jobs": count("my_jobs"), "my_running": count("my_running"),
            "partitions": parts}


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def connect_view(dev: Device, state: dict | None = None, *, here: bool = False) -> dict[str, Any]:
    """How to reach this device -- NEVER a secret.

    Anything returned here can land in an agent transcript, be persisted to disk, and be
    replayed in every later turn, so it says how to connect and nothing that lets anyone
    in: fleet holds no key or password, and the command is the one you would type.
    """
    from ..state.inventory import by_use
    eps = by_use(dev, state)
    if not eps:
        # The machine fleet runs on needs none, and "no endpoint recorded" under the
        # very first `fleet show` read as something to fix.
        return {"ssh_command": None,
                "hint": "this machine: run commands here" if here else "no endpoint recorded"}
    best = eps[0]
    if auth_of(dev, state) == "refused":
        return {"ssh_command": best.ssh_command(), "user": best.user, "port": best.port,
                "hint": f"It refused us last time. `fleet doctor {dev.name}` says why, and "
                        "the one command that lets you in."}
    return {"ssh_command": best.ssh_command(), "user": best.user, "port": best.port,
            "hint": f"Run it directly, or `fleet ssh {dev.name}`."}


def jobs_view(rows: list[dict]) -> dict:
    """`fleet ps`: every GPU job across the fleet, the idle GPUs, and who was not read."""
    jobs, idle, busy = [], [], []
    for r in rows:
        if r["status"] != "ok":
            continue
        for g in r["gpus"]:
            for j in g.get("jobs", []):
                jobs.append({"machine": r["name"], "gpu": g["idx"], "gpu_name": g["name"], **j})
            # Busy with nothing listed -- a job in a container, another user's: neither a
            # job nor idle, so it was left out of both and ps said "No jobs on any GPU."
            # beside a card at 97% holding 20G. A shared host's processes are left unread
            # on purpose, so its cards come marked as that, not as hidden: left out, a
            # login node's card at 97% read as "No jobs on any GPU." all the same.
            if not g.get("jobs") and g.get("busy"):
                busy.append({"machine": r["name"], "gpu": g["idx"], "gpu_name": g["name"],
                             "vram_used_mib": g["vram_used_mib"], "util_pct": g["util_pct"],
                             "shared_host": r.get("kind") == "shared"})
            # A shared host's, an appliance's: "never offered for work", and an agent
            # took the idle list for the GPUs it could start on.
            if not g.get("jobs") and not g.get("busy") and r.get("claimable", True):
                idle.append({"machine": r["name"], "gpu": g["idx"], "gpu_name": g["name"],
                             "vram_free_mib": g["vram_free_mib"],
                             "shared_memory": bool(g.get("shared_memory"))})
    return {"jobs": jobs, "idle": idle, "busy_unseen": busy,
            # Never read, its GPUs are not known to be none: `fleet ps gpu` on a machine
            # that was down said "No jobs on any GPU." and nothing else.
            "not_read": [r["name"] for r in rows if r["status"] != "ok"
                         and (r["gpu_count"] or not r.get("last_ok_at"))]}


def select_fields(row: dict, fields: list[str]) -> dict:
    """Only the keys asked for (and always `name`), for an agent that needs a few."""
    keep = ["name", *[f for f in fields if f != "name"]]
    return {k: row[k] for k in keep if k in row}


def models_view(rows: list[dict]) -> dict:
    """`fleet models`: every model server across the fleet, and the model folders."""
    # A machine that did not answer keeps its last reading, and that listed its servers
    # with a tunnel to each, as if they were there now: to a machine refusing us, the
    # tunnel offered could not even connect. It is named under not_read instead, as
    # `fleet ps` names it.
    read = [r for r in rows if r["status"] == "ok"]
    return {"servers": [{"machine": r["name"], **m} for r in read for m in r.get("models", [])],
            "model_folders": [{"machine": r["name"], **st} for r in read
                              for st in (r.get("storage") or {}).get("models", [])],
            # A machine that did not answer: "No model servers found" was all it said,
            # as if it had been looked at and serves nothing.
            "not_read": [r["name"] for r in rows if r["status"] != "ok"]}


def fleet_view(rows: list[dict]) -> dict:
    online = [r for r in rows if r["status"] == "ok"]
    # Up, though refusing us, is still running and still billed: a rental whose key had
    # gone read as "$0/hr", the one moment its cost mattered.
    up = [r for r in rows if r["status"] in (Status.OK.value, Status.AUTH_FAILED.value)]
    return {
        "devices": rows,
        "summary": {
            "total": len(rows),
            "online": len(online),
            "gpus_free": sum(1 for r in online if r.get("claimable", True)
                             for g in r.get("gpus", []) if not g.get("busy")),
            "rentals_running": sum(1 for r in up if r["kind"] == "rental"),
            "hourly_burn": round(sum(r.get("usd_per_hour") or 0 for r in up), 4),
        },
    }
