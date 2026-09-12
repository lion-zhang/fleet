"""THE canonical serializer.

Every surface -- `fleet ls --json`, the web /api, and every MCP tool -- renders through
this module. Downstream layers may only *subset* what it returns; none may compute a
field of its own. That makes "the dashboard and the agent disagree" a test failure
rather than a bug you find at 2am.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .models import Device, Kind, Status
from .store import age_s


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
                   "run `fleet key install`")
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
        "tags": list(dev.tags),
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
            "label": dev.label,
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
    from .inventory import endpoints_of
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
        return {"ssh_command": None, "auth": "password", "user": best.user, "port": best.port,
                "secret_ref": f"fleet://secret/{dev.name}",
                "hint": f"No working key for this host. Run `fleet ssh {dev.name}` for "
                        "the exact ssh-copy-id command. Credentials are never returned "
                        "here -- they would persist in the transcript."}
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
