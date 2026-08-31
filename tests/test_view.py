"""view.py is the single serializer every surface renders through. These tests pin the
contract so 'the dashboard and the agent disagree' becomes a test failure."""

from __future__ import annotations

import json
import pathlib

from fleet.models import Device, Kind
from fleet.probe.parse import parse_payload
from fleet.view import Detail, connect_view, device_view, fleet_view

FIX = pathlib.Path(__file__).parent / "fixtures" / "probe"


def snap_of(name: str) -> dict:
    return parse_payload((FIX / f"{name}.txt").read_text()).to_dict()


def make(name="gpu-box", kind=Kind.PERMANENT, **kw) -> Device:
    kw.setdefault("endpoints", [{"name": "primary", "target": "gpu-box.example.ts.net",
                                 "user": "lin", "port": 22}])
    return Device(id="linux:machine-id:abc", name=name, kind=kind, **kw)


STATE_OK = {"status": "ok", "last_probe_at": 10**9, "last_ok_at": 10**9,
            "error_class": "", "error_detail": ""}


# ------------------------------------------------------------------ the headline case
def test_idle_4090_is_reported_as_free():
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.COMPACT)
    assert v["gpu_count"] == 1
    assert v["free_vram_mib"] == 23197
    assert v["gpus"][0]["busy"] is False


def test_display_overhead_is_separated_from_compute():
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.FULL)
    g = v["gpus"][0]
    assert (g["compute_used_mib"], g["display_used_mib"], g["unattributed_mib"]) == (0, 439, 414)
    assert v["display_overhead_mib"] == 439


def test_unattributed_vram_raises_an_alert():
    """Never let an agent treat VRAM held by invisible processes as free."""
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.COMPACT)
    assert any("not visible" in a for a in v["alerts"])


def test_gpu_compute_list_excludes_cpu_processes():
    """Regression: tailscaled was being listed as a GPU job."""
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.FULL)
    assert v["processes"] == []                      # nothing is on the GPU
    assert "tailscaled" in {p["comm"] for p in v["top_cpu"]}


# ------------------------------------------------------------------ failure states
def test_auth_failed_is_distinguished_from_offline():
    state = {"status": "auth_failed", "last_probe_at": 10**9, "last_ok_at": None,
             "error_class": "auth_failed", "error_detail": "credentials rejected"}
    v = device_view(make("ds720"), state, None, Detail.COMPACT)
    assert v["status"] == "auth_failed"
    assert any("is UP" in a for a in v["alerts"])
    assert v["error"]["detail"] == "credentials rejected"


def test_failed_probe_keeps_last_known_good_timestamp():
    state = {"status": "refused", "last_probe_at": 10**9 + 500, "last_ok_at": 10**9,
             "error_class": "refused", "error_detail": "nothing listening"}
    v = device_view(make("blackwell", Kind.RENTAL), state, None, Detail.COMPACT)
    assert v["last_ok_at"] == 10**9        # degraded to stale-but-known, not blanked


def test_never_probed_device_has_null_age():
    v = device_view(make(), None, None, Detail.COMPACT)
    assert v["telemetry_age_s"] is None and v["status"] == "unknown"


# ------------------------------------------------------------------ policy surfaced in data
def test_shared_device_is_flagged_not_claimable():
    v = device_view(make("koa04", Kind.SHARED), STATE_OK, None, Detail.COMPACT)
    assert v["claimable"] is False
    assert any("not claimable" in a for a in v["alerts"])


def test_idle_rental_is_flagged_as_burning_money():
    v = device_view(make("blackwell", Kind.RENTAL, cost={"usd_per_hour": 1.20}),
                    STATE_OK, snap_of("gpu-box"), Detail.COMPACT)
    assert any("costing money" in a for a in v["alerts"])
    assert v["usd_per_hour"] == 1.20


def test_busy_rental_is_not_flagged():
    s = snap_of("gpu-box")
    s["gpus"][0]["util_pct"] = 95
    v = device_view(make("blackwell", Kind.RENTAL), STATE_OK, s, Detail.COMPACT)
    assert not any("costing money" in a for a in v["alerts"])


# ------------------------------------------------------------------ the security boundary
def test_connect_view_never_returns_a_password():
    """Anything returned here lands in an agent transcript and is replayed forever."""
    d = make("ds720", auth_state="needs_credentials")
    c = connect_view(d)
    assert c["ssh_command"] is None
    assert c["secret_ref"] == "fleet://secret/ds720"
    assert "fleet ssh ds720" in c["hint"]
    blob = json.dumps(device_view(d, STATE_OK, None, Detail.FULL)).lower()
    for banned in ("password", "passphrase", "secret_value", "hunter"):
        assert f'"{banned}":' not in blob


def test_full_view_does_not_leak_identity_file_paths_into_endpoints():
    d = make(endpoints=[{"name": "p", "target": "h", "user": "u", "port": 22,
                         "identity": "/home/u/.ssh/id_ed25519"}])
    v = device_view(d, STATE_OK, None, Detail.FULL)
    assert all("identity" not in e for e in v["endpoints"])


# ------------------------------------------------------------------ shape guarantees
def test_compact_view_omits_process_lists():
    """Process lists are the main context-blowout risk; list views must not carry them."""
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.COMPACT)
    assert "processes" not in v and "top_cpu" not in v and "disks" not in v


def test_view_is_json_serialisable_for_every_fixture():
    for name in ("gpu-box", "vm-a", "vm-b", "macos-laptop"):
        for detail in (Detail.COMPACT, Detail.FULL):
            json.dumps(device_view(make(), STATE_OK, snap_of(name), detail), default=str)


def test_view_is_deterministic():
    """Two renders of identical input must be byte-identical, or surfaces can drift."""
    a = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.FULL)
    b = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.FULL)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_fleet_summary_counts_and_burn_rate():
    rows = [
        device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.COMPACT),
        device_view(make("blackwell", Kind.RENTAL, cost={"usd_per_hour": 1.20}),
                    STATE_OK, snap_of("gpu-box"), Detail.COMPACT),
        device_view(make("ds720"), {"status": "auth_failed", "last_probe_at": 1,
                                    "error_class": "", "error_detail": ""}, None, Detail.COMPACT),
    ]
    s = fleet_view(rows)["summary"]
    assert (s["total"], s["online"], s["gpus_free"], s["hourly_burn"]) == (3, 2, 2, 1.2)


# ------------------------------------------------------------------ disk
def test_compact_view_carries_free_space_as_a_scalar():
    """`fleet ls` needs one number per device. The list of mounts is a full-view detail."""
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.COMPACT)
    assert v["disk_free_gb"] == 1647.5
    assert "disks" not in v


def test_the_compact_number_names_the_mount_it_came_from():
    """A bare 'disk free' would be a lie on a rental where / is a small overlay and
    /workspace holds the real storage. Say which mount the number describes."""
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.COMPACT)
    assert v["disk_mount"] == "/workspace"


def test_the_compact_number_is_the_roomiest_mount_not_the_root():
    root_free = next(d for d in snap_of("gpu-box")["disks"] if d["mount"] == "/")["avail_kb"]
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.COMPACT)
    assert v["disk_free_gb"] > round(root_free / 1048576, 1)


def test_a_device_with_one_mount_reports_that_one():
    v = device_view(make(), STATE_OK, snap_of("vm-a"), Detail.COMPACT)
    assert v["disk_mount"] == "/"
    assert v["disk_free_gb"] == 40.6


def test_a_device_with_no_telemetry_reports_no_disk_rather_than_zero():
    """Zero free would read as 'full'. Unknown must stay unknown."""
    v = device_view(make(), STATE_OK, None, Detail.COMPACT)
    assert v["disk_free_gb"] is None and v["disk_mount"] is None


def test_full_view_reports_disks_in_gb_with_a_usage_percentage():
    v = device_view(make(), STATE_OK, snap_of("gpu-box"), Detail.FULL)
    ws = next(d for d in v["disks"] if d["mount"] == "/workspace")
    assert ws["free_gb"] == 1647.5
    assert 0 < ws["use_pct"] < 10
    assert "total_kb" not in ws, "raw kb is a probe detail, not a view field"


def _full_disk_snapshot(pct_used: int) -> dict:
    total = 1000 * 1048576
    used = total * pct_used // 100
    return {"disks": [{"mount": "/workspace", "total_kb": total,
                       "used_kb": used, "avail_kb": total - used}]}


def test_a_nearly_full_disk_raises_an_alert():
    """A box that is 96% full looks perfectly healthy in `fleet ls` today, and the
    training run that fills it dies hours later."""
    v = device_view(make(), STATE_OK, _full_disk_snapshot(96), Detail.COMPACT)
    assert any("/workspace" in a and "disk" in a.lower() for a in v["alerts"])


def test_a_roomy_disk_raises_no_alert():
    v = device_view(make(), STATE_OK, _full_disk_snapshot(40), Detail.COMPACT)
    assert not any("disk" in a.lower() for a in v["alerts"])
