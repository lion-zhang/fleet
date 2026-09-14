"""Adding a machine is enrolling it.

There used to be two steps and a separate command: `fleet add` recorded a machine, and
`fleet center --enroll` put a key on it and pinned the key it answered with. Skipping the
second left a machine that was reachable and could be granted nothing -- the access list
is keyed on the fingerprint of *its* key, so an edge from it could not be expressed.

So `add` does both, wherever it can: on the center immediately, and on a spoke by leaving
the half only the center may do to the next sweep. What is left is the ordering that made
two commands necessary in the first place, and it is now enforced rather than documented:
a fleet exists before anything is added to it.
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from fleet import access as acl
from fleet import cli
from fleet import inventory as inv
from fleet import store
from fleet.models import Device, Kind, ProbeResult, Status


def _sandbox(tmp_path, monkeypatch):
    """Fleet state in a temp dir. CONFIG_DIR == STATE_DIR on macOS, so these four are
    distinct filenames in one directory and must each be redirected by name."""
    for name in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, name, tmp_path / getattr(acl, name).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    return CliRunner()


def _keypair(tmp_path, monkeypatch, name="center_ed25519"):
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    pub = key.with_suffix(".pub").read_text().strip()
    import fleet.config
    monkeypatch.setattr(fleet.config, "FLEET_KEY", key)
    monkeypatch.setattr(cli, "ensure_keypair", lambda *a, **k: (key, pub))
    return key, pub


def _probes(monkeypatch, dev, status=Status.OK, detail=""):
    monkeypatch.setattr(cli, "onboard", lambda *a, **k: (
        dev, ProbeResult(status=status, error_detail=detail)))


def _a_host(name="newbox", target="5.6.7.8", **kw):
    return Device(id=f"net:{target}:22", name=name, kind=Kind.RENTAL,
                  endpoints=[{"target": target, "user": "root", "port": 22}], **kw)


# ------------------------------------------------ the fleet comes first

def test_adding_before_a_fleet_exists_is_refused(tmp_path, monkeypatch):
    """The ordering that made a separate enrol command necessary. A machine is added
    *to* a fleet, so a machine in no fleet has nothing to add it to -- and the old
    behaviour, recording it anyway, is what produced hosts nobody could grant."""
    runner = _sandbox(tmp_path, monkeypatch)
    r = runner.invoke(cli.app, ["add", "ssh root@5.6.7.8"])
    assert r.exit_code == 2
    assert "not in a fleet" in r.output
    assert "fleet center --init" in r.output, "and says what to do about it"
    assert not inv.INVENTORY_PATH.exists(), "nothing recorded"


def test_a_spoke_can_add_but_leaves_enrolling_to_the_center(tmp_path, monkeypatch):
    """`fleet add` is not center-only. A spoke holds no access list -- only the center
    does -- so membership there is the signed cache the sweep leaves behind."""
    runner = _sandbox(tmp_path, monkeypatch)
    acl.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    acl.CACHE_PATH.write_text("center: SHA256:aaa\n")     # a spoke, not a center

    dev = _a_host()
    _probes(monkeypatch, dev)
    called = []
    monkeypatch.setattr(cli, "_register_identity", lambda d: called.append(d) or "fp")

    r = runner.invoke(cli.app, ["add", "ssh root@5.6.7.8"])
    assert r.exit_code == 0, r.output
    assert [d.name for d in inv.load(inv.INVENTORY_PATH)] == ["newbox"], "recorded"
    assert not called, "a spoke cannot write the access list, so it must not try"
    assert "next sweep" in r.output, "and must say the job is unfinished"


# ------------------------------------------------ it has to answer

@pytest.mark.parametrize("status", [Status.TIMEOUT, Status.UNREACHABLE, Status.REFUSED,
                                    Status.CLOSED, Status.HOST_KEY_MISMATCH])
def test_a_host_that_does_not_answer_is_not_recorded(tmp_path, monkeypatch, status):
    """Recording it would put an address nobody can reach in the inventory, and the
    first thing anyone did with it would fail. A changed host key belongs here too: it
    answered, but not as itself."""
    runner = _sandbox(tmp_path, monkeypatch)
    _keypair(tmp_path, monkeypatch)
    acl.save(acl.bootstrap("macbook", "ssh-ed25519 AAAA a", "id:me"), acl.ACCESS_PATH)

    _probes(monkeypatch, _a_host(), status=status, detail="no route")
    r = runner.invoke(cli.app, ["add", "ssh root@5.6.7.8"])
    assert r.exit_code == 1
    assert "did not answer" in r.output
    assert not inv.INVENTORY_PATH.exists(), "nothing recorded"


# ------------------------------------------------ the center finishes the job

def test_a_host_that_already_takes_our_key_is_enrolled_without_a_password(tmp_path,
                                                                          monkeypatch):
    """The case the docs promised and the code never delivered: a cloud VM with
    `PasswordAuthentication no` and a key in your agent. It used to ask for a password
    that did not exist and then fail."""
    runner = _sandbox(tmp_path, monkeypatch)
    _, pub = _keypair(tmp_path, monkeypatch)
    acl.save(acl.bootstrap("macbook", pub, "id:me"), acl.ACCESS_PATH)

    _probes(monkeypatch, _a_host(), status=Status.OK)
    asked = []
    monkeypatch.setattr(cli.getpass, "getpass",
                        lambda *a, **k: asked.append(1) or "x")
    monkeypatch.setattr(cli, "_register_identity", lambda d: "SHA256:new")

    r = runner.invoke(cli.app, ["add", "ssh root@5.6.7.8"])
    assert r.exit_code == 0, r.output
    assert not asked, "a host that already accepts our key must never be asked for one"


def test_a_host_authorized_upstream_gets_no_key(tmp_path, monkeypatch):
    """Tailscale SSH and friends authorize from their own ACL and never read
    authorized_keys, so writing one reports success and grants nothing."""
    runner = _sandbox(tmp_path, monkeypatch)
    _, pub = _keypair(tmp_path, monkeypatch)
    acl.save(acl.bootstrap("macbook", pub, "id:me"), acl.ACCESS_PATH)

    _probes(monkeypatch, _a_host(ssh_auth="external"), status=Status.OK)
    monkeypatch.setattr(cli, "_install_key",
                        lambda *a, **k: pytest.fail("must not install a key"))
    monkeypatch.setattr(cli, "_register_identity",
                        lambda d: pytest.fail("nothing to pin"))

    r = runner.invoke(cli.app, ["add", "ssh root@5.6.7.8"])
    assert r.exit_code == 0, r.output
    assert "upstream" in r.output


# ------------------------------------------------ the sweep is the retry path

def test_the_sweep_enrols_a_machine_that_has_no_pinned_key(tmp_path, monkeypatch):
    """What replaces the enrol command. A machine added from a spoke, or one whose
    enrolment was interrupted, is reachable and ungrantable until the center pins it."""
    runner = _sandbox(tmp_path, monkeypatch)
    _, pub = _keypair(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "local_device_id", lambda: "id:me")
    acc = acl.bootstrap("macbook", pub, "id:me")
    acl.save(acc, acl.ACCESS_PATH)
    inv.save([Device(id="id:me", name="macbook", kind=Kind.PERMANENT, role="center"),
              _a_host()], inv.INVENTORY_PATH)

    enrolled = []
    monkeypatch.setattr(cli, "_register_identity",
                        lambda d: enrolled.append(d.name) or "SHA256:new")
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    # the sweep ends by handing the inventory to every machine, which is one ssh each
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: (255, ""))

    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert enrolled == ["newbox"], "the unpinned machine, and only it"


def test_the_sweep_never_asks_for_a_password(tmp_path, monkeypatch):
    """It is unattended. A host that accepts no key from here is reported, not asked
    about -- the way out is `fleet center --pubkey`, not finding someone to type."""
    runner = _sandbox(tmp_path, monkeypatch)
    _, pub = _keypair(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "local_device_id", lambda: "id:me")
    acl.save(acl.bootstrap("macbook", pub, "id:me"), acl.ACCESS_PATH)
    inv.save([Device(id="id:me", name="macbook", kind=Kind.PERMANENT, role="center"),
              _a_host()], inv.INVENTORY_PATH)

    asked = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: asked.append(1) or "x")
    monkeypatch.setattr(cli, "_remote_pubkey_of", lambda *a, **k: "", raising=False)
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    # Let the real _register_identity run: it must fail without prompting.
    import fleet.reconcile as rec
    monkeypatch.setattr(rec, "_remote", lambda *a, **k: (False, "Permission denied"))

    runner.invoke(cli.app, ["sync"])
    assert not asked, "a sweep must never block on a prompt"


# ------------------------------------------------ the command is gone

def test_nothing_still_offers_an_enrol_command():
    """Deleting a command means deleting what points at it. Three messages did, plus one
    inside the handover, and each would have sent someone to a command that is not
    there."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    targets = (list((root / "src").rglob("*.py"))
               + list((root / "docs").rglob("*.md")) + [root / "README.md"])
    for path in targets:
        text = path.read_text()
        assert "--enroll" not in text, f"{path} still names a deleted flag"
        assert "--no-key-prompt" not in text, f"{path} still names a deleted flag"


# ------------------------------------------------ a name is not a guess

def test_rm_will_not_act_on_a_prefix(tmp_path, monkeypatch):
    """`inventory.find` resolves a unique prefix, which is right for `show`, `ssh` and
    `top` -- a wrong guess costs one re-run. It is wrong for the one command that cannot
    be undone, and an agent filling in a half-heard name is how those three letters
    arrive."""
    runner = _sandbox(tmp_path, monkeypatch)
    inv.save([_a_host(name="lin-xps")], inv.INVENTORY_PATH)

    r = runner.invoke(cli.app, ["rm", "lin", "-y"])
    assert r.exit_code == 1
    assert "exactly" in r.output
    assert "lin-xps" in r.output, "and names what you probably meant"
    assert [d.name for d in inv.load(inv.INVENTORY_PATH)] == ["lin-xps"], "still there"


def test_the_everyday_commands_still_take_a_prefix(tmp_path, monkeypatch):
    """The convenience is kept where a wrong guess is cheap."""
    _sandbox(tmp_path, monkeypatch)
    devices = [_a_host(name="lin-xps")]
    assert inv.find(devices, "lin").name == "lin-xps"
    assert inv.find_exact(devices, "lin") is None
    assert inv.find_exact(devices, "lin-xps").name == "lin-xps"


def test_json_still_enrols(tmp_path, monkeypatch):
    """An agent uses --json. A flag that quietly did half the command -- recording the
    machine but never enrolling it -- would be the worst kind of difference, because the
    machine looks added and can be granted nothing."""
    import json

    runner = _sandbox(tmp_path, monkeypatch)
    _, pub = _keypair(tmp_path, monkeypatch)
    acl.save(acl.bootstrap("macbook", pub, "id:me"), acl.ACCESS_PATH)

    _probes(monkeypatch, _a_host(), status=Status.OK)
    enrolled = []
    monkeypatch.setattr(cli, "_register_identity",
                        lambda d: enrolled.append(d.name) or "SHA256:new")

    r = runner.invoke(cli.app, ["add", "ssh root@5.6.7.8", "--json"])
    assert r.exit_code == 0, r.output
    assert enrolled == ["newbox"], "--json must not skip the half that makes it usable"
    payload = json.loads(r.stdout[r.stdout.index("{"):r.stdout.rindex("}") + 1])
    assert payload["enrolment"] == "enrolled", "and must report which state it is in"
