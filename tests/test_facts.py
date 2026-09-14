"""Tags you declare, facts fleet measures.

The split is the whole design. A hand-written `gpu` tag survives the card being pulled,
and the agent that trusted it sends a job to a machine with no GPU -- the same drift that
killed `Device.auth_state` and produced the rule in `view.auth_of`: a value with two
sources that disagree is worse than one that is recomputed.

So facts are recomputed from the last probe and never stored, and the asserts below are
mostly against the four real probe fixtures, which is what makes the macOS case a test
rather than a claim.
"""

from __future__ import annotations

import pathlib

import pytest

from fleet.render import view
from fleet.models import Device, Kind
from fleet.ops import identity
from fleet.probe.parse import parse_payload
from fleet.render.view import _CORES, _RAM_GIB, _SLACK, _STORAGE_TIB, _VRAM_GIB, facts, matches_tag
from fleet.ops import enrol
from fleet.ops import sweep
from fleet.ops import sync

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "probe"


def _snap(name: str) -> dict:
    return parse_payload((FIXTURES / f"{name}.txt").read_text()).to_dict()


def _dev(**kw) -> Device:
    kw.setdefault("kind", Kind.PERMANENT)
    kw.setdefault("name", "box")
    kw.setdefault("id", f"id:{kw['name']}")
    return Device(**kw)


def _facts(name: str, **kw) -> list[str]:
    return facts(_dev(**kw), _snap(name), None)


# ------------------------------------------------------------------ the ladders

def test_every_ladder_gap_survives_the_slack():
    """The safety proof for _SLACK, asserted rather than argued.

    Clearing a rung falsely needs the value to reach SLACK x the rung ABOVE, i.e. a gap
    below 1/SLACK. This fails the moment someone adds a rung too close to its neighbour.
    """
    for name, rungs in (("vram", _VRAM_GIB), ("ram", _RAM_GIB),
                        ("cores", _CORES), ("storage", _STORAGE_TIB)):
        tightest = min(b / a for a, b in zip(rungs, rungs[1:]))
        assert tightest > 1 / _SLACK, f"{name} rungs are too close for the slack"


def test_a_size_fact_means_at_least():
    """The downward closure, which is what makes the filter usable at all. One exact
    label could not answer "24G or more": --tag repeats as AND, so `--tag vram-24g
    --tag vram-48g` would match nothing and an agent would have to OR across the ladder
    itself."""
    f = facts(_dev(), {"gpus": [{"vram_total_mib": 49140}]}, None)
    assert "vram-48g" in f and "vram-24g" in f and "vram-8g" in f
    assert "vram-64g" not in f


def test_reported_capacity_runs_under_nominal():
    """Measured on real machines: a 32 GiB laptop reports 28.5, a 4 GiB VM reports 3.6,
    a 128 GiB workstation reports 122.9. Flooring to a rung tags the first `ram-16g` --
    wrong by a factor of two on the most-read fact in the set."""
    for reported_gib, expected in ((28.5, "ram-32g"), (3.6, "ram-4g"),
                                   (122.9, "ram-128g"), (64.0, "ram-64g")):
        f = facts(_dev(), {"mem_total_kb": int(reported_gib * 1048576)}, None)
        assert expected in f, f"{reported_gib} GiB should reach {expected}: {f}"


def test_a_genuinely_smaller_machine_is_not_promoted():
    """The slack must not reach the rung below. A 12 GiB VM is not a 16 GiB machine."""
    f = facts(_dev(), {"mem_total_kb": int(11.6 * 1048576)}, None)
    assert "ram-8g" in f and "ram-16g" not in f


# ------------------------------------------------------------------ accelerators

def test_vram_comes_from_the_largest_card_not_the_total():
    """A model fits in one card's VRAM or it does not; 4x24G is not a 96G machine. Same
    rule `free_vram_mib` and `gpu_cells` already follow."""
    f = facts(_dev(), {"gpus": [{"vram_total_mib": 24564}] * 4}, None)
    assert "multi-gpu" in f
    assert "vram-24g" in f and "vram-32g" not in f


def test_a_wedged_driver_still_owns_a_gpu():
    """gpu_present == "err" is nvidia-smi present but failing -- the card is almost
    certainly there and the list is empty. Saying nothing makes an expensive machine
    vanish from your own inventory; saying `cuda` sends a job to a box that dies at
    torch.cuda.init."""
    f = facts(_dev(), {"gpu_present": "err", "gpus": []}, None)
    assert "gpu" in f
    assert "cuda" not in f
    assert not [x for x in f if x.startswith("vram-")]


def test_gpu_facts_come_from_the_gpu_list_not_the_present_flag():
    """`gpu_present` flips to "1" when the driver-version query succeeds, before the
    per-GPU query returns, so "1" with an empty list is reachable."""
    assert facts(_dev(), {"gpu_present": "1", "gpus": []}, None) == []


# ------------------------------------------------------------------ the real fixtures

def test_the_macbook_reports_metal_and_never_cuda():
    """The probe only runs nvidia-smi, so a 40-core M3 Max GPU reports gpu.present=0.
    Without the inference the one machine that can run MLX is invisible; with a naive
    one it would claim CUDA."""
    f = _facts("macos-laptop")
    assert "metal" in f and "gpu" in f
    assert "cuda" not in f
    assert "macos" in f and "arm64" in f
    assert not [x for x in f if x.startswith("vram-")], "unified memory: no VRAM figure"


def test_the_gpu_box():
    f = _facts("gpu-box")
    assert "gpu" in f and "cuda" in f and "multi-gpu" not in f
    # 24564 MiB is 23.99 GiB -- it must reach the 24 rung and not the 32.
    assert "vram-24g" in f and "vram-32g" not in f
    assert "linux" in f and "x86_64" in f


def test_aarch64_is_normalised_to_arm64():
    """The fixture set already contains a spelling the naive vocabulary misses."""
    f = _facts("vm-a")
    assert "arm64" in f and "aarch64" not in f
    assert not [x for x in f if x in ("gpu", "cuda", "metal")]


@pytest.mark.parametrize("name", ["gpu-box", "vm-a", "vm-b", "macos-laptop"])
def test_every_fixture_gets_an_os_family(name):
    assert {"linux", "macos", "windows"} & set(_facts(name)), name


# ------------------------------------------------------------------ honesty

def test_a_machine_with_no_telemetry_has_no_facts():
    """Not a "no-data" marker: `--tag unknown` would then match it, mixing claims about
    the machine with claims about our knowledge of it."""
    assert facts(_dev(), None, None) == []


def test_facts_are_ordered_not_a_set():
    """String hashing is randomised per process, so a set would make `fleet ls --json`
    emit different bytes each run -- and the determinism test compares two calls inside
    one process, so it would pass while the property was broken."""
    snap = _snap("gpu-box")
    assert isinstance(facts(_dev(), snap, None), list)
    assert facts(_dev(), snap, None) == facts(_dev(), snap, None)


def test_only_unchanging_fields_are_used():
    """THE invariant. Free memory, free disk, utilisation and load all move between
    probes; a fact built on one would flap and poison every cached answer."""
    steady = {"mem_total_kb": 67108864, "cpu_cores": 16, "arch": "x86_64",
              "uname_s": "Linux", "gpus": [{"vram_total_mib": 24564}]}
    busy = steady | {"mem_avail_kb": 1, "users": 9, "load": [9.0, 9.0, 9.0],
                     "gpus": [{"vram_total_mib": 24564, "util_pct": 99}]}
    assert facts(_dev(), steady, None) == facts(_dev(), busy, None)


def test_storage_uses_writable_capacity_not_the_total():
    """_disk_view already warns that dividing by total understates fullness, and that
    macOS differs by tens of percent. Max over mounts, never sum: bind mounts and
    container overlays would be counted twice."""
    snap = {"disks": [
        {"used_kb": 1073741824, "avail_kb": 1073741824, "total_kb": 99999999999},
        {"used_kb": 536870912, "avail_kb": 536870912, "writable": True},
    ]}
    f = facts(_dev(), snap, None)
    assert "storage-2t" in f and "storage-4t" not in f, "max of 2 TiB, not the sum"


def test_a_read_only_mount_is_not_storage():
    snap = {"disks": [{"used_kb": 4294967296, "avail_kb": 0, "writable": False}]}
    assert not [x for x in facts(_dev(), snap, None) if x.startswith("storage-")]


# ------------------------------------------------------------------ reachability

def test_the_legacy_via_spelling_still_yields_mesh():
    """Endpoints on disk right now say `tailscale`. Reading the raw record in the facts
    while reading the normalised value everywhere else is how reachability came back
    empty for an entire fleet."""
    dev = _dev(endpoints=[{"target": "box.example.ts.net", "via": "tailscale"}])
    assert "mesh" in facts(dev, {}, None)


def test_a_route_is_backfilled_from_a_literal_address():
    """Records written before routes were classified still answer, with no DNS on a
    read path: a literal address or a known overlay suffix is enough."""
    assert "public-ip" in facts(_dev(endpoints=[{"target": "1.2.3.4"}]), {}, None)
    assert "lan" in facts(_dev(endpoints=[{"target": "192.168.1.9"}]), {}, None)
    assert facts(_dev(endpoints=[{"target": "nas.example.com"}]), {}, None) == []


def test_a_relayed_row_does_not_claim_a_route():
    """`mesh` and `lan` describe *our* route, and a broadcast row exists precisely
    because we have none. `public-ip` is a property of the machine, so it survives."""
    dev = _dev(endpoints=[{"target": "192.168.1.9"}])
    assert facts(dev, {}, {"source": "broadcast"}) == []

    pub = _dev(endpoints=[{"target": "1.2.3.4"}])
    assert "public-ip" in facts(pub, {}, {"source": "broadcast"})


# ------------------------------------------------------------------ tenancy

def test_tenancy_is_projected_from_kind_not_stored_again():
    """A derived view of one stored field cannot drift out of step with it, unlike a
    hand-typed `rental` tag."""
    assert "rental" in facts(_dev(kind=Kind.RENTAL), {}, None)
    assert "shared" in facts(_dev(kind=Kind.SHARED), {}, None)
    assert facts(_dev(kind=Kind.PERMANENT), {}, None) == []


# ------------------------------------------------------------------ the matcher

def test_one_namespace_matches_either_list():
    row = {"tags": ["prod"], "facts": ["gpu", "cuda"]}
    assert matches_tag(row, "prod") and matches_tag(row, "cuda")
    assert not matches_tag(row, "nas")


def test_the_filter_is_case_insensitive():
    """The request that prompted tags said "GPU", "NAS" and "IP"; facts are lowercase."""
    assert matches_tag({"tags": [], "facts": ["gpu"]}, "GPU")
    assert matches_tag({"tags": ["nas"], "facts": []}, "  NAS ")


def test_a_declared_tag_may_shadow_a_fact():
    """Deliberately allowed. A hand-set `gpu` on a box whose nvidia-smi is wedged, or an
    accelerator fleet has no probe for, is the correct use of the field -- and the YAML
    is hand-editable, so a CLI-only refusal would be a rule the file format ignores."""
    assert matches_tag({"tags": ["gpu"], "facts": []}, "gpu")
    assert view.is_fact_name("gpu") and view.is_fact_name("vram-24g")
    assert not view.is_fact_name("prod")


# ------------------------------------------------------------------ the commands

def _cli(tmp_path, monkeypatch, devices):
    from typer.testing import CliRunner

    from fleet import cli
    from fleet.state import inventory as inv, store

    path = tmp_path / "inventory.yaml"
    inv.save(devices, path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(identity, "local_device_id", lambda: "")
    return CliRunner(), cli


def test_edit_adds_and_removes_tags(tmp_path, monkeypatch):
    from fleet.state import inventory as inv

    runner, cli = _cli(tmp_path, monkeypatch, [_dev(name="box")])
    assert runner.invoke(cli.app, ["edit", "box", "--tag", "prod", "--tag", "nas"]).exit_code == 0
    assert inv.load(inv.INVENTORY_PATH)[0].tags == ["prod", "nas"]

    assert runner.invoke(cli.app, ["edit", "box", "--untag", "prod"]).exit_code == 0
    assert inv.load(inv.INVENTORY_PATH)[0].tags == ["nas"]


def test_tags_are_lowercased_on_write(tmp_path, monkeypatch):
    """The request said "GPU", "NAS", "IP". Storing those verbatim beside lowercase
    facts would make `--tag nas` miss the machine the user just tagged."""
    from fleet.state import inventory as inv

    runner, cli = _cli(tmp_path, monkeypatch, [_dev(name="box")])
    runner.invoke(cli.app, ["edit", "box", "--tag", "NAS"])
    assert inv.load(inv.INVENTORY_PATH)[0].tags == ["nas"]


def test_adding_the_same_tag_twice_is_not_a_change(tmp_path, monkeypatch):
    from fleet.state import inventory as inv

    runner, cli = _cli(tmp_path, monkeypatch, [_dev(name="box", tags=["prod"])])
    before = inv.load(inv.INVENTORY_PATH)[0].updated_at
    runner.invoke(cli.app, ["edit", "box", "--tag", "prod"])
    after = inv.load(inv.INVENTORY_PATH)[0]
    assert after.tags == ["prod"]
    assert after.updated_at == before, "a no-op must not win the next merge"


def test_ls_filters_on_tags_and_facts(tmp_path, monkeypatch):
    from fleet import cli as cli_mod

    devices = [_dev(name="a", tags=["prod"]), _dev(name="b", tags=["nas"])]
    runner, cli = _cli(tmp_path, monkeypatch, devices)
    monkeypatch.setattr(cli_mod, "_rows", lambda *a, **k: [
        {"name": "a", "tags": ["prod"], "facts": ["gpu", "cuda"], "status": "ok"},
        {"name": "b", "tags": ["nas"], "facts": ["linux"], "status": "ok"},
    ])
    monkeypatch.setattr(cli_mod, "fleet_view", lambda rows: {"devices": rows, "summary": {}})

    out = runner.invoke(cli.app, ["ls", "--tag", "cuda", "--json"]).stdout
    assert '"a"' in out and '"b"' not in out
    out = runner.invoke(cli.app, ["ls", "--tag", "nas", "--json"]).stdout
    assert '"b"' in out and '"a"' not in out
    # repeats are AND
    out = runner.invoke(cli.app, ["ls", "--tag", "cuda", "--tag", "nas", "--json"]).stdout
    assert '"a"' not in out and '"b"' not in out


def test_ls_says_what_it_could_not_judge(tmp_path, monkeypatch):
    """A machine with no telemetry fails every filter, and the whole fleet is factless
    right after a cache wipe. Silence would read as "nothing suitable exists"."""
    from fleet import cli as cli_mod

    runner, cli = _cli(tmp_path, monkeypatch, [_dev(name="a")])
    monkeypatch.setattr(cli_mod, "_rows", lambda *a, **k: [
        {"name": "a", "tags": [], "facts": [], "status": "timeout"},
    ])
    monkeypatch.setattr(cli_mod, "fleet_view", lambda rows: {"devices": rows, "summary": {}})
    r = runner.invoke(cli.app, ["ls", "--tag", "gpu"])
    assert "not considered" in r.output and "a" in r.output


# ------------------------------------------------------------------ portability

def test_fleet_imports_without_a_pty():
    """A center may run on Windows, where `pty` imports `tty` imports `termios` and
    there is no termios. A top-level import took the whole CLI down at startup, so
    fleet could not even print --version there."""
    import importlib
    import sys

    blocked = {"pty": None, "tty": None, "termios": None}
    saved = {k: sys.modules.get(k) for k in blocked}
    try:
        sys.modules.update(blocked)          # import of these now raises ImportError
        for mod in ("fleet.ssh.keys", "fleet.cli"):
            importlib.reload(importlib.import_module(mod))
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        for mod in ("fleet.ssh.keys", "fleet.cli"):
            importlib.reload(importlib.import_module(mod))


def test_a_machine_without_a_pty_says_so_instead_of_crashing(tmp_path, monkeypatch):
    """The one genuinely unportable path. It must read as a limit of this machine, and
    name the way round it, rather than surfacing as an ImportError mid-enrolment."""
    from fleet import cli

    monkeypatch.setattr(enrol, "pty_available", lambda: False)
    monkeypatch.setattr(enrol, "ensure_keypair", lambda *a, **k: (tmp_path / "k", "ssh-ed25519 AAAA x"))
    monkeypatch.setattr(enrol, "install_key_over_existing_access",
                        lambda *a, **k: (False, "Permission denied (publickey)."))
    monkeypatch.setattr(enrol.getpass, "getpass",
                        lambda *a, **k: pytest.fail("must not prompt without a pty"))

    dev = _dev(name="box", endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])
    assert enrol.install_our_key(dev) is False


def test_the_console_forces_utf8_output():
    """A Windows console encodes cp1252 and rich raises rather than degrading, so
    `fleet center --init` created a fleet and then died printing the checkmark that said
    so. The glyphs carry meaning -- which machine is the center, which one is this -- so
    the streams are reconfigured rather than the marks dropped."""
    import io
    import sys

    class Cp1252(io.TextIOBase):
        encoding = "cp1252"
        reconfigured: dict = {}

        def reconfigure(self, **kw):
            Cp1252.reconfigured = kw

    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = Cp1252()
        import importlib
        importlib.reload(importlib.import_module("fleet.ui"))
        assert Cp1252.reconfigured.get("encoding") == "utf-8"
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        import importlib
        importlib.reload(importlib.import_module("fleet.ui"))


def test_the_local_probe_uses_powershell_on_windows():
    """A center probes itself. On Windows the POSIX payload finds no /etc/machine-id, so
    `derive_id` fell back to `net:localhost:22` -- the id that means "never probed" --
    and the access list pinned the center under the one identity that is not stable."""
    from fleet.probe import runner

    seen = {}

    def fake_run(argv, **kw):
        # bytes now: text mode would rewrite every \n to \r\n on a Windows center and
        # the far-side `sh` would answer `Syntax error: "|" unexpected`
        raw = kw.get("input", b"")
        assert isinstance(raw, bytes), "the payload must not go through text mode"
        seen["argv"], seen["input"] = argv, raw.decode()
        raise OSError("stop here; the choice of payload is the thing under test")

    import unittest.mock as mock
    for platform, expect in (("windows", "powershell"), ("posix", "sh")):
        with mock.patch.object(runner, "local_platform", lambda p=platform: p), \
             mock.patch.object(runner, "local_shell_argv",
                               lambda p=platform: ["powershell", "-NoProfile", "-Command", "-"]
                               if p == "windows" else ["sh", "-s"]), \
             mock.patch.object(runner.subprocess, "run", fake_run):
            runner.run_probe_local()
        assert seen["argv"][0] == expect
        assert "FLEET" in seen["input"]


def test_one_function_answers_which_os_this_is():
    """There were three separate `sys.platform == "win32"` tests and they had already
    drifted in shape, which is how the local probe kept running the POSIX payload on a
    Windows center. Local and remote now answer in the same vocabulary."""
    from fleet.ssh.cmd import POSIX, WINDOWS, local_platform, local_shell_argv, remote_platform

    assert local_platform() in (POSIX, WINDOWS)
    assert local_shell_argv()[0] in ("sh", "powershell")
    # uname_s is preferred over the marketing string, and the sniff remains for
    # snapshots taken before it was parsed
    assert remote_platform({"uname_s": "Windows"}) == WINDOWS
    assert remote_platform({"uname_s": "Linux", "os": "Microsoft Windows 11 Pro"}) == POSIX
    assert remote_platform({"os": "Microsoft Windows 11 Pro"}) == WINDOWS
    assert remote_platform(None) == POSIX


def test_enrolment_records_whose_authorized_keys_to_write():
    """`edges()` fell back to root for every machine, so a center kept trying to write
    root's file on hosts only ever reached as an ordinary user, and every grant sat
    pending behind a permission denial naming the wrong account."""
    from fleet.state import access as acl

    # Built in memory, never via bootstrap(): that resolves the real ACCESS_PATH and
    # would pin a center into this machine's actual config directory.
    acc = acl.Access(fleet_id="7f3a9c", center="SHA256:center",
                     keys={"SHA256:center": {"name": "center", "pubkey": "x"}})
    # Distinct base64 blobs, not distinct comments: a fingerprint hashes the key itself,
    # so `ssh-ed25519 AAAA n` and `ssh-ed25519 AAAA v` are the same key wearing two names.
    fp = acl.enroll(acc, "nas", "ssh-ed25519 AAAB", "id:n", user="lin")
    assert acc.keys[fp]["user"] == "lin"
    assert (acc.center, fp, "lin") in acc.edges()

    # unspecified still means root, which is the old behaviour and the common case
    fp2 = acl.enroll(acc, "vm", "ssh-ed25519 AAAC", "id:v")
    assert fp2 != fp
    assert (acc.center, fp2, "root") in acc.edges()


def test_an_identity_path_from_another_machine_is_ignored(tmp_path):
    """`identity` is a filename on whichever machine recorded the endpoint, and the
    inventory syncs. Handing ssh a path this machine does not have fails the whole
    connection, where no path at all just falls back to the fleet key."""
    from fleet.state import inventory as inv
    from fleet.models import Device, Kind

    real = tmp_path / "id_ed25519"
    real.write_text("x")
    dev = Device(id="id:a", name="a", kind=Kind.PERMANENT, endpoints=[
        {"target": "h1", "identity": "/nowhere/this/does/not/exist"},
        {"target": "h2", "identity": str(real)},
    ])
    got = {e.target: e.identity for e in inv.endpoints_of(dev)}
    assert got["h1"] == "", "a foreign path must not reach ssh"
    assert got["h2"] == str(real), "a real one still does"


def test_reinitialising_does_not_rename_the_center(tmp_path, monkeypatch):
    """--init counted the machine's own inventory record among the taken names, so a
    second run came back as "<name>-2" and pinned that into the access list while the
    inventory kept the first."""
    from typer.testing import CliRunner

    from fleet.state import access as acl, inventory as inv, store
    from fleet import cli
    from fleet.models import Device, Kind, ProbeResult, Status

    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    key = tmp_path / "k"
    import subprocess
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)], check=True)
    pub = key.with_suffix(".pub").read_text().strip()
    monkeypatch.setattr(enrol, "ensure_keypair", lambda *a, **k: (key, pub))
    monkeypatch.setattr(cli, "onboard_self", lambda **k: (
        Device(id="id:me", name="beelink", kind=Kind.PERMANENT), ProbeResult(status=Status.OK)))

    runner = CliRunner()
    assert runner.invoke(cli.app, ["center", "--init"]).exit_code == 0
    acl.ACCESS_PATH.unlink()                       # as --dissolve leaves it
    assert runner.invoke(cli.app, ["center", "--init"]).exit_code == 0

    acc = acl.load(acl.ACCESS_PATH)
    assert acc.name_of(acc.center) == "beelink", "still itself, not beelink-2"
    assert [d.name for d in inv.live(inv.load(inv.INVENTORY_PATH))] == ["beelink"]


def test_a_probe_payload_keeps_its_newlines():
    """Popen's text mode wraps stdin with newline=None, which rewrites \\n to \\r\\n on
    Windows. A POSIX center therefore sent payload.sh verbatim and a Windows one sent it
    CRLF, and every remote `sh` answered `Syntax error: "|" unexpected`. There is no
    newline= on Popen, so the payload is encoded by hand and must stay bytes."""
    import unittest.mock as mock

    from fleet.probe import runner
    from fleet.ssh.cmd import Endpoint

    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self, payload=None, timeout=None):
            seen["payload"] = payload
            return b"", b""

        def kill(self): pass

    with mock.patch.object(runner.subprocess, "Popen", lambda *a, **k: FakeProc()):
        runner._run_probe_once(Endpoint(target="h"), timeout=1)
    assert isinstance(seen["payload"], bytes)
    assert b"\r\n" not in seen["payload"], "CRLF would break the remote shell"


def test_a_windows_machine_knows_its_own_id(monkeypatch):
    """It returned "" there, so a Windows center did not recognise its own row: it tried
    to ssh to itself and reported "device has no endpoints" about the machine it was
    running on. The value must match what derive_id stamps from payload.ps1, which reads
    the same registry key."""
    import sys
    import types

    fake = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=lambda *a: __import__("contextlib").nullcontext(),
        QueryValueEx=lambda key, name: ("665aebe1-5029-45c5-adae-035a2b4fada7", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(identity, "local_platform", lambda: "windows")
    # lru_cache(maxsize=1): without clearing on both sides this answer leaks into every
    # test that runs after it, and the failure surfaces somewhere else entirely.
    identity.local_device_id.cache_clear()
    try:
        assert identity.local_device_id() == "linux:machine-id:665aebe1-5029-45c5-adae-035a2b4fada7"
    finally:
        identity.local_device_id.cache_clear()


def test_init_marks_the_center_in_the_inventory_too(tmp_path, monkeypatch):
    """`is_center()` stays the authority, but the diamond in `ls` and `top` is drawn from
    Device.role -- so without this the center was invisible in the one view that exists
    to answer "which machine decides"."""
    from typer.testing import CliRunner

    from fleet.state import access as acl, inventory as inv, store
    from fleet import cli
    from fleet.models import Device, Kind, ProbeResult, Status

    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    import subprocess
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)], check=True)
    monkeypatch.setattr(enrol, "ensure_keypair",
                        lambda *a, **k: (key, key.with_suffix(".pub").read_text().strip()))
    monkeypatch.setattr(cli, "onboard_self", lambda **k: (
        Device(id="id:me", name="hub", kind=Kind.PERMANENT), ProbeResult(status=Status.OK)))

    assert CliRunner().invoke(cli.app, ["center", "--init"]).exit_code == 0
    assert inv.find_exact(inv.load(inv.INVENTORY_PATH), "hub").role == "center"


def test_no_remote_script_goes_through_text_mode():
    """Popen and run() wrap stdin with newline=None in text mode, rewriting \\n to \\r\\n
    on Windows. That broke three things at once from the new Windows center: the probe
    payload (`sh: Syntax error: "|" unexpected`), the authorized_keys edit (which wrote
    its replacement to a temp file and never reached the `mv` -- had it, the file would
    have been replaced by our block alone), and SSHSIG signing (covering bytes no spoke
    ever sees, so every verify would fail).

    A source guard rather than a behaviour test: the next `input=` added without
    `.encode()` is the one that breaks a fleet nobody is watching."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "fleet"
    offenders = []
    for path in src.rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            # `payload_for` is the installer's byte source -- it returns base64 for
            # Windows and encoded sh for POSIX, and test_install asserts both are bytes.
            if re.search(r"\binput=", line) and ".encode()" not in line \
                    and "read_bytes()" not in line and "payload_for(" not in line \
                    and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "remote payloads must be bytes:\n" + "\n".join(offenders)


def test_the_sweep_hands_the_inventory_to_every_machine(tmp_path, monkeypatch):
    """The center dials out and nothing dials in, so a spoke only ever learns who the
    center is if the center tells it. Without this it held our key with no idea where it
    came from: `fleet ls` there could not mark the center, and `fleet sync` there had
    nobody to ask."""
    from typer.testing import CliRunner

    from fleet.state import access as acl, inventory as inv, store
    from fleet import cli, reconcile as rec
    from fleet.models import Device, Kind

    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:center")

    devices = [
        Device(id="id:center", name="hub", kind=Kind.PERMANENT, role="center"),
        Device(id="id:a", name="a", kind=Kind.PERMANENT,
               endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}]),
    ]
    inv.save(devices, inv.INVENTORY_PATH)
    acc = acl.Access(fleet_id="7f3a9c", center="SHA256:c", keys={
        "SHA256:c": {"name": "hub", "pubkey": "x", "device_id": "id:center"},
        "SHA256:a": {"name": "a", "pubkey": "y", "device_id": "id:a", "user": "root"},
    })
    acl.save(acc, acl.ACCESS_PATH)

    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(enrol, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    handed = []
    monkeypatch.setattr(sync, "run_sync",
                        lambda ep, payload: handed.append(ep.target) or (0, payload))

    assert CliRunner().invoke(cli.app, ["sync"]).exit_code == 0
    assert handed == ["1.2.3.4"], "every machine with an endpoint, and only those"


def test_a_machine_without_fleet_is_not_an_error(tmp_path, monkeypatch):
    """Most managed targets have nothing installed. The handover failing there is the
    ordinary case, not a fault worth a line of output."""
    from fleet import cli
    from fleet.state import inventory as inv
    from fleet.models import Device, Kind

    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    devices = [Device(id="id:a", name="a", kind=Kind.PERMANENT,
                      endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])]
    inv.save(devices, inv.INVENTORY_PATH)
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: (127, "fleet: command not found"))
    sweep.broadcast(devices)                # must not raise


def test_a_settled_fleet_still_hands_the_inventory_round(tmp_path, monkeypatch):
    """Keys converging is the common case, and the early return for it skipped the
    handover entirely -- so a fleet that had finished setting itself up was precisely
    the one that never told its machines anything."""
    from typer.testing import CliRunner

    from fleet.state import access as acl, inventory as inv, store
    from fleet import cli
    from fleet.models import Device, Kind

    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:center")

    devices = [Device(id="id:center", name="hub", kind=Kind.PERMANENT, role="center"),
               Device(id="id:a", name="a", kind=Kind.PERMANENT,
                      endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])]
    inv.save(devices, inv.INVENTORY_PATH)
    # no edges at all, so nothing is ever pending
    acl.save(acl.Access(fleet_id="7f3a9c", center="SHA256:c", keys={
        "SHA256:c": {"name": "hub", "pubkey": "x", "device_id": "id:center"}}),
        acl.ACCESS_PATH)

    handed = []
    monkeypatch.setattr(sync, "run_sync",
                        lambda ep, payload: handed.append(ep.target) or (0, payload))
    monkeypatch.setattr(sweep, "enrol_unpinned", lambda *a, **k: False)

    assert CliRunner().invoke(cli.app, ["sync"]).exit_code == 0
    assert handed == ["1.2.3.4"]
