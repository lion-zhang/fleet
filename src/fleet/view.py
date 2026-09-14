"""THE canonical serializer.

Every surface -- `fleet ls --json`, the web /api, and every MCP tool -- renders through
this module. Downstream layers may only *subset* what it returns; none may compute a
field of its own. That makes "the dashboard and the agent disagree" a test failure
rather than a bug you find at 2am.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from .models import Device, Kind, Status
from .ssh.cmd import route_of
from .state.store import age_s


class Detail(StrEnum):
    COMPACT = "compact"     # list views; no process lists
    FULL = "full"           # single-device views


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
        "busy": g["util_pct"] >= 10 or compute >= 1024,
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
        out.append("cuda")
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

    used+avail, never total_kb: filesystems reserve blocks nobody can write, and per
    _disk_view above macOS differs by tens of percent. Max rather than sum, because
    bind mounts and container overlays would be counted twice.
    """
    sizes = [(d.get("used_kb", 0) + d.get("avail_kb", 0)) / 1073741824
             for d in snap.get("disks") or [] if d.get("writable", True)]
    return max(sizes, default=0.0)


def _reachability(dev: Device, state: dict | None) -> list[str]:
    """How this machine is reached, from the endpoints recorded for it.

    `public-ip` is a property of the machine and survives being relayed. `mesh` and `lan`
    describe *our route*, and a broadcast row exists precisely because we have none, so
    claiming one there would assert reachability we demonstrably lack.
    """
    vias = {route_of(e.get("via", ""), e.get("target", "")) for e in dev.endpoints}
    out = ["public-ip"] if "public" in vias else []
    if (state or {}).get("source", "self") == "self":
        out += [v for v in ("mesh", "lan") if v in vias]
    return out


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
    "gpu", "cuda", "metal", "multi-gpu",
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


def _disk_view(d: dict) -> dict:
    """One mount, in the units a human and an agent both read.

    Usage is computed against used+avail, not total: filesystems reserve blocks the
    caller can never write, and dividing by total quietly understates how full a disk
    is. macOS is the extreme case, where the two differ by tens of percent.
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


def auth_of(dev: Device, state: dict | None) -> str:
    """Whether key auth works here. Derived, never stored.

    This used to be a `Device.auth_state` field, set once at onboarding and cleared only
    by a successful key install. Nothing moved it back to "ok" after a good probe and
    nothing moved it to "needs_credentials" after a later failure, so it drifted away
    from the truth in both directions and routinely disagreed with the cached probe
    status -- which is why the password retry keyed off the probe and ignored the field.
    A value with two sources that disagree is worse than one that is recomputed.

    A device nobody has probed yet reads "ok": "we have no reason to think this will
    fail" is the right default, and it is what the field defaulted to anyway.
    """
    if dev.ssh_auth == "external":
        # The network authorizes here, not authorized_keys. A refusal is a policy
        # decision upstream, and offering to install a key would be advice that cannot
        # work -- there is no file on this host that would change the answer.
        return "external"
    if state and state.get("status") == Status.AUTH_FAILED.value:
        return "needs_key"
    return "ok"


def _alerts(dev: Device, snap: dict | None, state: dict | None) -> list[str]:
    out: list[str] = []
    auth = auth_of(dev, state)
    if auth == "needs_key":
        out.append("host is UP but rejected our key (this is not 'offline') -- "
                   "the center installs one with `fleet access`")
    elif auth == "external" and state and state.get("status") == Status.AUTH_FAILED.value:
        out.append("host is UP but the network refused us -- authorization for this one "
                   "lives upstream, not in authorized_keys")
    if snap:
        for g in snap.get("gpus", []):
            if _gpu_view(g)["unattributed_mib"] >= 256:
                out.append(f"gpu{g['idx']}: {_gpu_view(g)['unattributed_mib']} MiB held by "
                           "processes not visible to us -- do not treat as free")
        for disk in snap.get("disks", []):
            dv = _disk_view(disk)
            # a read-only mount is 100% full by definition; nothing will ever die on it
            if dv["writable"] and dv["use_pct"] >= DISK_ALERT_PCT:
                out.append(f"disk {dv['mount']}: {dv['use_pct']}% full, "
                           f"{dv['free_gb']}G left -- a long job will die on this")
        if snap.get("gpu_present") == "err":
            out.append(f"nvidia-smi present but failing: {snap.get('gpu_error', '')[:80]}")
    if dev.kind is Kind.SHARED:
        out.append("shared multi-user host -- not claimable")
    if dev.kind is Kind.RENTAL and snap:
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

    out: dict[str, Any] = {
        "name": dev.name,
        "id": dev.id,
        "alias": dev.alias,
        "kind": dev.kind.value,
        "role": dev.role,
        "status": status,
        "claimable": dev.claimable,
        # Requires both to be non-empty: a container with no machine-id has no identity,
        # and claiming to be some device would be worse than admitting we cannot tell.
        "is_self": bool(self_id and dev.id and dev.id == self_id),
        "telemetry_age_s": age_s(state),
        "gpus": [{"name": g["name"], "vram_total_mib": g["vram_total_mib"],
                  "vram_free_mib": g["vram_free_mib"], "util_pct": g["util_pct"],
                  "busy": g["busy"]} for g in gpus] if detail is Detail.COMPACT else gpus,
        "gpu_count": len(gpus),
        "free_vram_mib": max((g["vram_free_mib"] for g in gpus), default=0),
        "ssh_auth": auth_of(dev, state),
        # where this row came from: a probe we ran, or one the center relayed
        # for a machine we cannot reach ourselves
        "source": (state or {}).get("source") or "self",
        "probed_by": (state or {}).get("probed_by") or "",
        "cpu_cores": (snap or {}).get("cpu_cores"),
        "ram_total_gb": round(mem_total / 1048576, 1) if mem_total else None,
        "ram_free_gb": round(mem_avail / 1048576, 1) if mem_avail else None,
        # the roomiest mount stays, as the one a job should be pointed at
        "disk_free_gb": roomiest["free_gb"] if roomiest else None,
        "disk_mount": roomiest["mount"] if roomiest else None,
        # ...but every mount is listed beside it, because reducing to one number hid a
        # rental's nearly full / behind its big /workspace -- and the mount that ends a
        # long job is precisely the hidden one. Trimmed to mount+free at COMPACT, the
        # way `gpus` is, so `fleet ls --json` stays small for the agents reading it.
        "disks": ([{"mount": d["mount"], "free_gb": d["free_gb"]} for d in disks]
                  if detail is Detail.COMPACT else disks),
        "usd_per_hour": (dev.cost or {}).get("usd_per_hour"),
        "services": [{"name": s.get("name"), "kind": s.get("kind"), "port": s.get("port"),
                      "healthy": s.get("healthy")}
                     for s in (snap or {}).get("services", [])],
        "alerts": _alerts(dev, snap, state),
        "tags": [normalise_tag(t) for t in dev.tags],
        "facts": facts(dev, snap, state),
        # How old the *hardware reading* is, which is not telemetry_age_s: store.latest
        # answers state and snapshot with two independent queries, so a device probed
        # 4s ago to a timeout can carry a three-day-old broadcast snapshot underneath a
        # `source: self` row. Facts read as timeless claims, so they need their own
        # denominator.
        "snapshot_age_s": (int(time.time()) - snap["ts"]) if snap and snap.get("ts") else None,
    }
    if state and status != Status.OK.value:
        out["error"] = {"class": state.get("error_class"), "detail": state.get("error_detail")}
        out["last_ok_at"] = state.get("last_ok_at")

    if detail is Detail.FULL:
        procs = (snap or {}).get("processes", [])
        # "compute processes" means work occupying the GPU. A busy CPU process is a
        # different question and gets its own list; conflating them made `fleet show`
        # report tailscaled as a GPU job.
        compute = [p for p in procs
                   if p.get("scope") == "gpu" and p.get("klass") == "compute"]
        top_cpu = sorted((p for p in procs if p.get("scope") == "cpu"),
                         key=lambda p: -p.get("cpu_pct", 0))[:5]
        display_mib = sum(p.get("vram_mib", 0)
                          for p in procs if p.get("klass") == "display")
        out |= {
            "alias": dev.alias,
            "notes": dev.notes,
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
            "top_cpu": top_cpu,
            "display_overhead_mib": display_mib,
            "services_detail": (snap or {}).get("services", []),
            "endpoints": [{k: v for k, v in e.items() if k != "identity"}
                          for e in dev.endpoints],
            "connect": connect_view(dev, state),
            "probe_policy": dev.probe_policy,
            "provider": dev.provider,
            "cost": dev.cost,
        }
    return out


def connect_view(dev: Device, state: dict | None = None) -> dict[str, Any]:
    """How to reach this device -- NEVER the secret itself.

    Anything returned here can land in an agent transcript, be persisted to disk, and be
    replayed in every later turn. A password that crosses this boundary is permanently in
    a log nobody will remember to scrub, and the agent gains nothing: it does not need to
    see the credential, only for the connection to work.
    """
    from .state.inventory import endpoints_of
    eps = sorted(endpoints_of(dev), key=lambda e: e.preference)
    if not eps:
        return {"ssh_command": None, "auth": "none", "hint": "no endpoint recorded"}
    best = eps[0]
    if auth_of(dev, state) == "external":
        return {"ssh_command": best.ssh_command(), "auth": "external",
                "user": best.user, "port": best.port,
                "hint": "This host authorizes upstream, not from authorized_keys. "
                        "Access is granted in the network's own policy; fleet neither "
                        "installs nor removes keys here."}
    if auth_of(dev, state) == "needs_key":
        return {"ssh_command": None, "auth": "needs_key", "user": best.user,
                "port": best.port,
                "hint": f"This host has not accepted our key yet. The center installs "
                        f"one with `fleet access {dev.name} --allow <machine>`; nothing "
                        "here is ever a credential."}
    return {"ssh_command": best.ssh_command(), "auth": "key",
            "user": best.user, "port": best.port,
            "hint": f"Key auth. Run it directly, or `fleet ssh {dev.name}`."}


def fleet_view(rows: list[dict]) -> dict:
    online = [r for r in rows if r["status"] == "ok"]
    return {
        "devices": rows,
        "summary": {
            "total": len(rows),
            "online": len(online),
            "gpus_free": sum(1 for r in online for g in r.get("gpus", []) if not g.get("busy")),
            "rentals_running": sum(1 for r in online if r["kind"] == "rental"),
            "hourly_burn": round(sum(r.get("usd_per_hour") or 0 for r in online), 2),
        },
    }
