"""Draw the README's screenshots with fleet's own renderers.

    uv run python scripts/readme_screenshots.py

Builds a throwaway fleet in a temp directory -- invented machines, made-up hostnames, no
addresses -- records a probe snapshot for each in the same wire format the real probe
emits, and renders `fleet ls` and `fleet top` exactly as a user sees them. Re-run it
whenever the output changes: the images are generated, never hand-edited.

Writes docs/assets/fleet-ls.svg, docs/assets/fleet-top.svg and
docs/assets/social-preview.svg.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"

# Before fleet is imported: it reads these at import time.
_tmp = Path(tempfile.mkdtemp(prefix="fleet-readme-"))
os.environ["FLEET_CONFIG_DIR"] = str(_tmp / "config")
os.environ["FLEET_STATE_DIR"] = str(_tmp / "state")
sys.path.insert(0, str(REPO / "src"))

from rich.console import Console, Group  # noqa: E402
from rich.text import Text  # noqa: E402

from fleet.models import Device, Kind, ProbeResult, Status  # noqa: E402
from fleet.ops import identity  # noqa: E402
from fleet.probe.parse import parse_payload  # noqa: E402
from fleet.render.top import render_fleet, render_ls  # noqa: E402
from fleet.render.view import Detail, fleet_view  # noqa: E402
from fleet.state import inventory as inv  # noqa: E402
from fleet.state import store  # noqa: E402

GB = 1024 * 1024          # kB per GB


def payload(host: str, mid: str, *, cores: int, ram_gb: float, free_gb: float,
            gpus: list[tuple[str, int, int, int]] = (), procs: list[tuple[int, int, str]] = (),
            disks: list[tuple[str, int, int]] = (("/", 500, 120),), arch: str = "x86_64",
            os_name: str = "Ubuntu 24.04 LTS", load: str = "0.40 0.30 0.20",
            vast: str = "") -> str:
    """One probe's output, in payload.sh's wire format.

    gpus: (name, total_mib, used_mib, util_pct). procs: (gpu index, vram_mib, comm) --
    the processes holding VRAM, so used memory is attributed rather than flagged.
    disks: (mount, total_gb, used_gb).
    """
    lines = ["#FLEET v1", "probe.mode=full", f"host.hostname={host}", "host.uname_s=Linux",
             f"host.arch={arch}", "host.kernel=6.8.0", f"host.machine_id={mid}",
             f"host.os={os_name}", "host.uptime_s=864000", "host.users=1",
             "host.is_container=0", f"host.vast_label={vast}", "host.runpod_id=",
             "host.autodl=0", "host.slurm=0", f"cpu.cores={cores}",
             "cpu.model=AMD EPYC 7763 64-Core Processor", f"cpu.load={load}",
             f"mem.total_kb={int(ram_gb * GB)}", f"mem.avail_kb={int(free_gb * GB)}",
             f"gpu.present={1 if gpus else 0}", "gpu.driver=570.86" if gpus else "gpu.driver=",
             "gpu.error=", "#DISK mount|total_kb|used_kb|avail_kb"]
    for mount, total, used in disks:
        lines.append(f"{mount}|{total * GB}|{used * GB}|{(total - used) * GB}")
    lines.append("#GPU idx|uuid|name|total_mib|used_mib|free_mib|util_pct|temp_c|power_w")
    for i, (name, total, used, util) in enumerate(gpus):
        lines.append(f"{i}|GPU-{mid[:8]}-{i:04d}|{name}|{total}|{used}|{total - used}|"
                     f"{util}|{40 + util // 3}|{60 + util * 3}")
    lines.append("#GPUPROC gpu_uuid|pid|vram_mib|user|etimes|comm")
    for n, (idx, mib, comm) in enumerate(procs):
        lines.append(f"GPU-{mid[:8]}-{idx:04d}|{4000 + n}|{mib}|you|7200|{comm}")
    lines += ["#CPUPROC pid|user|pcpu|rss_kb|etimes|comm", "#LISTEN proto|addr|port|pid|comm",
              "#END rc=0"]
    return "\n".join(lines) + "\n"


def mac(host: str, mid: str) -> str:
    text = (REPO / "tests" / "fixtures" / "probe" / "macos-laptop.txt").read_text()
    out = []
    for line in text.splitlines():
        if line.startswith("host.hostname="):
            line = f"host.hostname={host}"
        elif line.startswith("host.machine_id="):
            line = f"host.machine_id={mid}"
        out.append(line)
    return "\n".join(out) + "\n"


def endpoint(target: str) -> list[dict]:
    return [{"name": "primary", "target": target, "user": "you", "port": 22}]


# The fleet in the picture: what someone with a few GPU boxes and a couple of rentals has.
MACHINES = [
    (Device(id="d:a100", name="a100-spot", kind=Kind.RENTAL, tags=["spot"],
            cost={"usd_per_hour": 1.89}, endpoints=endpoint("a100-spot.example")),
     payload("a100-spot", "a1" * 16, cores=32, ram_gb=250, free_gb=212,
             gpus=[("NVIDIA A100-SXM4-80GB", 81920, 66100, 97),
                   ("NVIDIA A100-SXM4-80GB", 81920, 3, 0)],
             procs=[(0, 66100, "python")],
             disks=[("/", 100, 31), ("/workspace", 2000, 640)], vast="")),
    (Device(id="d:h100", name="h100-idle", kind=Kind.RENTAL,
            cost={"usd_per_hour": 2.49}, endpoints=endpoint("h100.example")),
     payload("h100-idle", "b2" * 16, cores=26, ram_gb=200, free_gb=190,
             gpus=[("NVIDIA H100 80GB HBM3", 81559, 4, 0)],
             disks=[("/", 200, 12), ("/workspace", 1000, 80)])),
    (Device(id="d:4090", name="rtx4090", kind=Kind.PERMANENT, tags=["home"],
            endpoints=endpoint("rtx4090.example.ts.net")),
     payload("rtx4090", "c3" * 16, cores=32, ram_gb=64, free_gb=51,
             gpus=[("NVIDIA GeForce RTX 4090", 24564, 860, 1)],
             procs=[(0, 860, "Xorg")], disks=[("/", 1800, 410)])),
    (Device(id="d:3090", name="lab-3090", kind=Kind.PERMANENT, alias="lab",
            endpoints=endpoint("lab-3090.example")),
     payload("lab-3090", "d4" * 16, cores=24, ram_gb=128, free_gb=40, load="11.2 10.8 9.9",
             gpus=[("NVIDIA GeForce RTX 3090", 24576, 22900, 88),
                   ("NVIDIA GeForce RTX 3090", 24576, 23100, 91)],
             procs=[(0, 22900, "python"), (1, 23100, "python")],
             disks=[("/", 900, 820)])),
    (Device(id="d:mbp", name="macbook", kind=Kind.MOBILE, endpoints=[]), mac("macbook", "e5" * 16)),
    (Device(id="d:nas", name="nas", kind=Kind.APPLIANCE, tags=["nas", "backup"],
            endpoints=endpoint("nas.example.ts.net")),
     payload("nas", "f6" * 16, cores=4, ram_gb=16, free_gb=11, arch="aarch64",
             disks=[("/", 32, 9), ("/volume1", 16000, 9200)])),
    (Device(id="d:old", name="old-rig", kind=Kind.PERMANENT,
            endpoints=endpoint("old-rig.example")), None),
]


def build() -> list[dict]:
    from fleet.ops import rows as rows_mod

    devices = [d for d, _ in MACHINES]
    inv.save(devices)
    conn = store.connect()
    for dev, text in MACHINES:
        if text is None:
            store.record(conn, dev.id, ProbeResult(
                status=Status.TIMEOUT, error_class="timeout",
                error_detail="no answer in 8s -- last seen 3d ago"))
        else:
            store.record(conn, dev.id, ProbeResult(status=Status.OK,
                                                   snapshot=parse_payload(text)))
    conn.close()
    identity.local_device_id = lambda: "d:mbp"       # drawn from the laptop's point of view
    rows_mod._probe = lambda *a, **k: None            # never dial: these hosts are invented
    return rows_mod.snapshot(detail=Detail.FULL)


def console(width: int = 150) -> Console:
    import io

    # The tables' natural widths (measured: ls 147, top 132). Narrower and rich starts
    # truncating names and figures, which is not what anyone's terminal shows them.
    return Console(record=True, width=width, force_terminal=True, color_system="truecolor",
                   legacy_windows=False, file=io.StringIO())   # recorded, not printed


def footer(summary: dict, extra: str = "") -> Text:
    return Text.from_markup(
        f"\n[dim]{summary['online']}/{summary['total']} online · {summary['gpus_free']} "
        f"free GPU(s)" + (f" · ${summary['hourly_burn']:.2f}/hr burning"
                          if summary["hourly_burn"] else "") + extra + "[/dim]")


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    rows = build()
    view = fleet_view(rows)

    c = console()
    c.print("[bold green]$[/bold green] fleet ls")
    c.print(render_ls(rows))
    c.print(footer(view["summary"]))
    (ASSETS / "fleet-ls.svg").write_text(c.export_svg(title="fleet ls"))

    c = console(136)
    c.print(Group(render_fleet(rows, view["summary"], 10),
                  footer(view["summary"], "  ·  q quit   r refresh")))
    (ASSETS / "fleet-top.svg").write_text(c.export_svg(title="fleet top"))

    c = console(150)
    # Just the table: scripts/social_preview.html sets the title around it.
    c.print("[bold green]$[/bold green] fleet ls --tag gpu")
    c.print(render_ls([r for r in rows if r["name"] in
                       ("a100-spot", "h100-idle", "rtx4090", "lab-3090")]))
    (ASSETS / "social-preview.svg").write_text(c.export_svg(title="fleet"))
    print(f"wrote {', '.join(p.name for p in sorted(ASSETS.glob('*.svg')))} to {ASSETS}")


if __name__ == "__main__":
    main()
