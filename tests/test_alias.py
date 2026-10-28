"""A short handle you can type instead of the name.

Names come from the host and are often long (`lin-workstation`, `macbook-m3-max`), so
the thing you type most is the thing least convenient to type. An alias fixes that
without touching identity: the id is still what merge and the access list key on, and
the name is still what everything renders.

The one rule that makes it safe is that aliases and names share a namespace. An alias
that shadowed another machine's name would be ambiguous in exactly the place it is used
most -- `fleet ssh x`, `fleet rm x` -- so it is refused.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.ops import identity
from fleet.state import inventory as inv
from fleet.state import store
from fleet.edit import apply_edits
from fleet.models import Device, Kind


def _devices():
    return [
        Device(id="id:xps", name="lin-xps", kind=Kind.PERMANENT, alias="x"),
        Device(id="id:ws", name="lin-workstation", kind=Kind.PERMANENT),
    ]


def _cli(tmp_path, monkeypatch, devices=None):
    path = tmp_path / "inventory.yaml"
    inv.save(devices if devices is not None else _devices(), path)
    monkeypatch.setattr(inv, "INVENTORY_PATH", path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    return CliRunner()


# ------------------------------------------------------------------ resolving

def test_an_alias_resolves_wherever_a_name_does():
    devices = _devices()
    assert inv.find(devices, "x").name == "lin-xps"
    assert inv.find_exact(devices, "x").name == "lin-xps"


def test_an_alias_counts_as_exact():
    """`fleet rm` takes exact handles only, and an alias is one: the user chose it for
    this machine and typed it in full. What stays refused is a guessed-at prefix."""
    devices = _devices()
    assert inv.find_exact(devices, "x").name == "lin-xps"
    assert inv.find_exact(devices, "lin") is None, "a prefix is still not exact"


def test_the_name_still_wins_over_a_stale_alias():
    """A name and another machine's alias cannot collide -- that is refused at the point
    of setting -- but the lookup order is still worth pinning."""
    devices = [Device(id="id:a", name="x", kind=Kind.PERMANENT),
               Device(id="id:b", name="other", kind=Kind.PERMANENT, alias="x")]
    assert inv.find_exact(devices, "x").id == "id:a"


# ------------------------------------------------------------------ the namespace

def test_an_alias_cannot_shadow_another_machines_name():
    """The whole point is typing it where a name goes, so one that shadowed a name would
    be ambiguous exactly where it is most used."""
    dev = Device(id="id:ws", name="lin-workstation", kind=Kind.PERMANENT)
    with pytest.raises(ValueError, match="already answers to"):
        apply_edits(dev, alias="lin-xps", taken={"lin-xps", "x"})


def test_an_alias_cannot_shadow_another_machines_alias():
    dev = Device(id="id:ws", name="lin-workstation", kind=Kind.PERMANENT)
    with pytest.raises(ValueError, match="already answers to"):
        apply_edits(dev, alias="x", taken={"lin-xps", "x"})


def test_an_alias_equal_to_its_own_name_is_refused():
    """It buys nothing and reads as a mistake worth reporting."""
    dev = Device(id="id:xps", name="lin-xps", kind=Kind.PERMANENT)
    with pytest.raises(ValueError, match="not an alias"):
        apply_edits(dev, alias="lin-xps", taken=set())


def test_a_machine_keeps_its_own_alias_when_edited():
    """`handles(excluding=...)` leaves this machine out, or re-setting the same alias --
    or any other edit that passes it through -- would collide with itself."""
    devices = _devices()
    taken = inv.handles(devices, excluding="id:xps")
    assert "x" not in taken and "lin-xps" not in taken
    assert "lin-workstation" in taken


# ------------------------------------------------------------------ setting it

def test_edit_sets_and_clears_an_alias(tmp_path, monkeypatch):
    runner = _cli(tmp_path, monkeypatch)
    r = runner.invoke(cli.app, ["edit", "lin-workstation", "--alias", "w"])
    assert r.exit_code == 0, r.output
    assert inv.find(inv.load(inv.INVENTORY_PATH), "w").name == "lin-workstation"

    r = runner.invoke(cli.app, ["edit", "w", "--alias", ""])
    assert r.exit_code == 0, r.output
    assert inv.find_exact(inv.load(inv.INVENTORY_PATH), "w") is None, "cleared"


def test_edit_finds_the_machine_by_its_alias(tmp_path, monkeypatch):
    """Otherwise the handle works for reading and not for changing, which is the more
    annoying half."""
    runner = _cli(tmp_path, monkeypatch)
    r = runner.invoke(cli.app, ["edit", "x", "--name", "renamed"])
    assert r.exit_code == 0, r.output
    names = {d.name for d in inv.load(inv.INVENTORY_PATH)}
    assert "renamed" in names and "lin-xps" not in names


def test_adding_with_a_taken_alias_is_refused(tmp_path, monkeypatch):
    from fleet.models import ProbeResult, Status

    runner = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "onboard", lambda *a, **k: (
        Device(id="net:1.2.3.4:22", name="newbox", kind=Kind.RENTAL),
        ProbeResult(status=Status.OK)))

    r = runner.invoke(cli.app, ["add", "ssh root@1.2.3.4", "--alias", "x"])
    assert r.exit_code == 2
    assert "already answers to" in r.output
    assert {d.name for d in inv.load(inv.INVENTORY_PATH)} == {"lin-xps", "lin-workstation"}


# ------------------------------------------------------------------ round trip and display

def test_an_alias_survives_the_inventory_file():
    """`_payload` drops falsy values, so an unset alias costs no line -- but a set one
    has to come back."""
    text = inv.dumps(_devices())
    assert "alias: x" in text
    assert inv.find_exact(inv.loads(text), "x").name == "lin-xps"


def test_an_unset_alias_writes_nothing():
    text = inv.dumps([Device(id="id:a", name="plain", kind=Kind.PERMANENT)])
    assert "alias" not in text


def test_the_alias_is_shown_beside_the_name():
    from fleet.render.top import name_cell

    assert "(x)" in name_cell({"name": "lin-xps", "alias": "x"})
    assert "(" not in name_cell({"name": "lin-xps", "alias": ""})


def test_looking_at_a_machine_by_its_alias(tmp_path, monkeypatch):
    """`ls`, `show` and `top` all filter through `_rows`, which matched on name and id
    only -- so the alias worked for `ssh` and `edit` and not for the thing you do most.
    Found by using it, not by testing the units it is made of."""
    _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(identity, "local_device_id", lambda: "")
    rows = cli._rows(["x"], refresh=False)
    assert [r["name"] for r in rows] == ["lin-xps"]


def test_a_name_and_an_alias_can_be_mixed_in_one_filter(tmp_path, monkeypatch):
    _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(identity, "local_device_id", lambda: "")
    rows = cli._rows(["x", "lin-workstation"], refresh=False)
    assert {r["name"] for r in rows} == {"lin-xps", "lin-workstation"}


def test_a_clone_sharing_a_machine_id_is_kept_apart(tmp_path, monkeypatch):
    """Found on a real fleet: two containers from one image, one machine-id. The second
    was merged into the first as "one device, not two"."""
    from typer.testing import CliRunner

    from fleet import cli
    from fleet.models import Device, Kind, ProbeResult, Snapshot, Status
    from fleet.state import inventory as inv
    from fleet.state import store

    inv.save([Device(id="linux:machine-id:same", name="clone3", kind=Kind.PERMANENT,
                     endpoints=[{"target": "clone3", "user": "root", "port": 22}])])
    conn = store.connect()
    store.record(conn, "linux:machine-id:same",
                 ProbeResult(status=Status.OK, snapshot=Snapshot(ts=1, hostname="clone3")))
    conn.close()

    def onboard(cmd, **k):
        dev = Device(id="linux:machine-id:same", name="clone4", kind=Kind.PERMANENT,
                     endpoints=[{"target": "clone4", "user": "root", "port": 22}])
        return dev, ProbeResult(status=Status.OK, snapshot=Snapshot(ts=2, hostname="clone4"))

    monkeypatch.setattr(cli, "onboard", onboard)
    # the known machine does not answer now: only its last reading can tell
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: ProbeResult(status=Status.TIMEOUT))
    monkeypatch.setattr(cli, "_run_script", lambda *a, **k: (True, "ok"))
    r = CliRunner().invoke(cli.app, ["add", "ssh root@clone4"])
    assert r.exit_code == 0, r.output
    names = {d.name for d in inv.live(inv.load())}
    assert names == {"clone3", "clone4"}, names
    assert "cloned from the same image" in r.output


def test_one_box_reached_two_ways_is_still_one_record(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fleet import cli
    from fleet.models import Device, Kind, ProbeResult, Snapshot, Status
    from fleet.state import inventory as inv
    from fleet.state import store

    inv.save([Device(id="linux:machine-id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    conn = store.connect()
    store.record(conn, "linux:machine-id:box",
                 ProbeResult(status=Status.OK, snapshot=Snapshot(ts=1, hostname="box")))
    conn.close()
    monkeypatch.setattr(cli, "onboard", lambda cmd, **k: (
        Device(id="linux:machine-id:box", name="box-2", kind=Kind.PERMANENT,
               endpoints=[{"target": "box.example.ts.net", "user": "root", "port": 22}]),
        ProbeResult(status=Status.OK, snapshot=Snapshot(ts=2, hostname="box"))))
    # the known machine does not answer now: only its last reading can tell
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: ProbeResult(status=Status.TIMEOUT))
    monkeypatch.setattr(cli, "_run_script", lambda *a, **k: (True, "ok"))
    r = CliRunner().invoke(cli.app, ["add", "ssh root@box.example.ts.net"])
    assert r.exit_code == 0, r.output
    assert [d.name for d in inv.live(inv.load())] == ["box"]


def test_a_known_box_added_again_reports_the_name_it_keeps(tmp_path, monkeypatch):
    """An agent reads the name from `fleet add --json` and uses it next. Reporting the
    requested name for a machine already known as another sent it after a machine that
    did not exist ("No device named 'loop'"), found running fleet on a real runner."""
    import json

    from typer.testing import CliRunner

    from fleet import cli
    from fleet.models import Device, Kind, ProbeResult, Snapshot, Status
    from fleet.state import inventory as inv
    from fleet.state import store

    inv.save([Device(id="linux:machine-id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    conn = store.connect()
    store.record(conn, "linux:machine-id:box",
                 ProbeResult(status=Status.OK, snapshot=Snapshot(ts=1, hostname="box")))
    conn.close()
    monkeypatch.setattr(cli, "onboard", lambda cmd, **k: (
        Device(id="linux:machine-id:box", name=k.get("name") or "box-2", kind=Kind.PERMANENT,
               endpoints=[{"target": "localhost", "user": "me", "port": 22}]),
        ProbeResult(status=Status.OK, snapshot=Snapshot(ts=2, hostname="box"))))
    # the known machine does not answer now: only its last reading can tell
    monkeypatch.setattr(cli, "run_probe", lambda *a, **k: ProbeResult(status=Status.TIMEOUT))
    monkeypatch.setattr(cli, "_run_script", lambda *a, **k: (True, "ok"))
    r = CliRunner().invoke(cli.app, ["add", "ssh me@localhost", "--name", "loop", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["action"] == "endpoint_added" and out["name"] == "box", out

    r = CliRunner().invoke(cli.app, ["add", "ssh me@localhost", "--name", "loop"])
    assert "fleet edit box --name loop" in r.output, r.output


def _readd(monkeypatch, snapshot=None):
    """`fleet add` reaching a machine whose id is already recorded."""
    from fleet.models import ProbeResult, Snapshot, Status
    from fleet.onboard import classify_kind, rental_provider
    from fleet.ssh.cmd import Endpoint

    snap = snapshot or Snapshot(ts=2, hostname="box")

    def onboard(cmd, **k):
        dev = Device(id="linux:machine-id:box", name=k.get("name") or "box",
                     kind=classify_kind(snap, Endpoint(target="10.0.0.5"),
                                        override=k.get("kind")),
                     endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}],
                     provider=rental_provider(snap))
        return dev, ProbeResult(status=Status.OK, snapshot=snap)

    monkeypatch.setattr(cli, "onboard", onboard)
    monkeypatch.setattr(cli, "_run_script", lambda *a, **k: (True, "ok"))


def test_re_adding_a_removed_machine_stores_the_kind_it_was_given(tmp_path, monkeypatch):
    """`fleet rm box --yes; fleet add ... --kind rental` said "added box (rental, ok)"
    and kept `kind: permanent`: the restored record won, and the message described the
    one that was thrown away. Found recording the launch demo."""
    import json

    runner = _cli(tmp_path, monkeypatch, [
        Device(id="linux:machine-id:box", name="box", kind=Kind.PERMANENT,
               endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    _readd(monkeypatch)
    assert runner.invoke(cli.app, ["rm", "box", "--yes"]).exit_code == 0
    r = runner.invoke(cli.app, ["add", "ssh root@10.0.0.5", "--name", "box",
                                "--kind", "rental", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["kind"] == "rental"
    assert inv.find_exact(inv.load(), "box").kind is Kind.RENTAL

    # and unsaid, the record keeps what it was: a re-add is no way to forget a kind
    r = runner.invoke(cli.app, ["add", "ssh root@10.0.0.5"])
    assert "(rental, ok)" in r.output, r.output
    assert inv.find_exact(inv.load(), "box").kind is Kind.RENTAL


def test_a_machine_that_answers_as_a_vast_instance_is_a_rental(tmp_path, monkeypatch):
    """A vast.ai box recorded as permanent once (removed, a reused machine-id) stayed
    permanent on every later add, though the probe knew where it ran: no idle-rental
    alert, no burn rate."""
    from fleet.models import Snapshot

    runner = _cli(tmp_path, monkeypatch, [
        Device(id="linux:machine-id:box", name="box", kind=Kind.PERMANENT,
               endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}],
               deleted_at=1)])
    _readd(monkeypatch, Snapshot(ts=2, hostname="box", vast_label="C.12345678"))
    r = runner.invoke(cli.app, ["add", "ssh root@10.0.0.5"])
    assert r.exit_code == 0, r.output
    box = inv.find_exact(inv.load(), "box")
    assert (box.kind, box.provider) == (Kind.RENTAL, "vastai")
    assert "(rental, ok)" in r.output, r.output


def test_runpod_and_autodl_are_known_providers_too():
    from fleet.models import Snapshot
    from fleet.onboard import rental_provider

    assert rental_provider(Snapshot(ts=1, runpod_id="abc")) == "runpod"
    assert rental_provider(Snapshot(ts=1, autodl=True)) == "autodl"
    assert rental_provider(Snapshot(ts=1)) == ""


def test_a_dry_run_on_a_known_machine_answers_as_the_real_add_would(tmp_path, monkeypatch):
    """`fleet add "ssh root@cloud-1" --dry-run` said `"name": "cloud-1-2"` -- the name a
    new machine would get -- while the real add left `cloud-1` unchanged."""
    import json

    runner = _cli(tmp_path, monkeypatch, [
        Device(id="linux:machine-id:box", name="box", kind=Kind.PERMANENT,
               endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    _readd(monkeypatch)
    real = cli.onboard
    monkeypatch.setattr(cli, "onboard", lambda cmd, **k: real(cmd, **{**k, "name": "box-2"}))
    r = runner.invoke(cli.app, ["add", "ssh root@10.0.0.5", "--dry-run"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert (out["action"], out["name"]) == ("unchanged", "box"), out


def test_adding_a_known_machine_does_not_also_say_it_was_added(tmp_path, monkeypatch):
    runner = _cli(tmp_path, monkeypatch, [
        Device(id="linux:machine-id:box", name="box", kind=Kind.PERMANENT,
               endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    _readd(monkeypatch)
    r = runner.invoke(cli.app, ["add", "ssh root@10.0.0.5"])
    assert "already recorded" in r.output and "added" not in r.output, r.output
    assert runner.invoke(cli.app, ["rm", "box", "--yes"]).exit_code == 0
    r = runner.invoke(cli.app, ["add", "ssh root@10.0.0.5", "--name", "other"])
    assert "restored box" in r.output and "added" not in r.output, r.output
    assert "fleet edit box --name other" in r.output, "it comes back as box, and says so"


def test_untag_removes_a_tag_that_add_kept_here(tmp_path, monkeypatch):
    """`add --tag web` keeps the tag in this machine's list; `edit --untag web` only
    edited the machine's own file, said "already says that", and `ls` still showed it."""
    from fleet.ops import machinefile

    runner = _cli(tmp_path, monkeypatch, [
        Device(id="id:box", name="box", kind=Kind.PERMANENT, tags=["web"],
               endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    monkeypatch.setattr(machinefile, "change", lambda *a, **k: ("", ""))
    r = runner.invoke(cli.app, ["edit", "box", "--untag", "web"])
    assert r.exit_code == 0, r.output
    assert inv.find_exact(inv.load(), "box").tags == []


def test_an_edit_that_changes_both_places_prints_one_json_document(tmp_path, monkeypatch):
    """A note goes to the machine's file and a new name to this list. Each half printed
    its own document, and two back to back are not JSON: the MCP server's edit_machine
    handed the agent raw text. `--untag` of a tag kept here did the same."""
    import json

    from fleet.ops import machinefile

    runner = _cli(tmp_path, monkeypatch, [
        Device(id="id:box", name="box", kind=Kind.PERMANENT, tags=["web"],
               endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    monkeypatch.setattr(machinefile, "change", lambda *a, **k: ("", "notes: hi\n"))
    for args in (["--note", "hi", "--name", "box2"], ["--untag", "web"]):
        name = inv.load()[0].name
        r = runner.invoke(cli.app, ["edit", name, *args, "--json"])
        assert r.exit_code == 0, r.output
        doc = json.loads(r.stdout)
        assert doc["ok"] and doc["changes"], doc
    assert doc["name"] == "box2" and doc["notes"] == "hi"
    assert inv.find_exact(inv.load(), "box2").tags == []


def test_cost_0_also_clears_a_cost_kept_here(tmp_path, monkeypatch):
    """`--cost 0` cleared the machine's file and said "cleared", while a cost set with
    --local stayed, and `ls` went on showing the machine costing that much."""
    from fleet.ops import machinefile

    runner = _cli(tmp_path, monkeypatch, [
        Device(id="id:box", name="box", kind=Kind.RENTAL, cost={"usd_per_hour": 2.0},
               endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])])
    monkeypatch.setattr(machinefile, "change", lambda *a, **k: ("cost: 1.5\n", ""))
    r = runner.invoke(cli.app, ["edit", "box", "--cost", "0"])
    assert r.exit_code == 0, r.output
    assert not (inv.find_exact(inv.load(), "box").cost or {}).get("usd_per_hour")


def test_a_prefix_that_fits_several_machines_says_which(tmp_path, monkeypatch):
    runner = _cli(tmp_path, monkeypatch, [
        Device(id="id:1", name="cloud-1", kind=Kind.PERMANENT),
        Device(id="id:2", name="cloud-2", kind=Kind.PERMANENT)])
    r = runner.invoke(cli.app, ["ssh", "cloud", "--", "true"])
    assert r.exit_code == 1 and "did you mean: cloud-1, cloud-2" in r.output, r.output


def test_a_field_that_does_not_exist_is_said_to_not_exist(tmp_path, monkeypatch):
    """`ls --json --fields nosuchfield` came back as rows holding only `name`, which
    reads as "no machine has it" rather than "you mistyped it"."""
    runner = _cli(tmp_path, monkeypatch)
    r = runner.invoke(cli.app, ["ls", "--json", "--fields", "alias,nosuchfield"])
    assert r.exit_code == 0, r.output
    assert "no such field: nosuchfield " in " ".join(r.stderr.split()), r.stderr
