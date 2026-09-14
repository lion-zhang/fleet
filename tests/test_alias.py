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
    monkeypatch.setattr(cli, "_fleet_membership", lambda: "center")
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
    from fleet.top import name_cell

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
