"""Desired vs observed, and the guards around acting on it."""

from __future__ import annotations

from fleet import reconcile
from fleet.access import Access
from fleet.reconcile import EdgeState, plan, refuses_to_run

A, B, C = "SHA256:aaa", "SHA256:bbb", "SHA256:ccc"


def _acc(**kw):
    return Access(fleet_id="7f3a9c", center=A,
                  keys={A: {"name": "macbook", "pubkey": "ssh-ed25519 AAAA a"},
                        B: {"name": "lin-xps", "pubkey": "ssh-ed25519 AAAA b"},
                        C: {"name": "oracle", "pubkey": "ssh-ed25519 AAAA c"}}, **kw)


def test_the_centers_edges_appear_without_being_written():
    led = plan(_acc(), {})
    assert set(led) == {f"{A}>{B}>root", f"{A}>{C}>root"}
    assert all(st.desired == "present" for st in led.values())


def test_a_dropped_edge_becomes_work_rather_than_silence():
    """"This key should not be there" is a thing to do, not an absence to forget."""
    led = plan(_acc(), {f"{B}>{C}>root": EdgeState(desired="present", observed="present")})
    assert led[f"{B}>{C}>root"].desired == "absent"
    assert led[f"{B}>{C}>root"].observed == "present", "still installed until we look"


def test_planning_is_idempotent():
    acc = _acc()
    once = plan(acc, {})
    twice = plan(acc, once)
    assert {k: v.desired for k, v in once.items()} == {k: v.desired for k, v in twice.items()}


def test_an_unconverged_edge_knows_it():
    st = EdgeState(desired="absent", observed="present")
    assert st.converged is False
    assert EdgeState(desired="present", observed="present").converged is True


def test_removing_everything_at_once_is_refused():
    """An empty list means "strip every key fleet placed, everywhere". Reaching that in
    one step is always a bug upstream -- an unreadable file, a half-written save -- and
    there is no way back from acting on it."""
    empty = Access(fleet_id="7f3a9c", center="", keys={})
    installed = {f"{A}>{B}>root": EdgeState(observed="present")}
    assert refuses_to_run(empty, installed)
    assert not refuses_to_run(_acc(), installed), "a real list is fine"
    assert not refuses_to_run(empty, {}), "nothing installed, nothing to lose"


def test_only_a_pinned_key_is_ever_installed(tmp_path):
    """Device.pubkey rides the ordinary merge, so a peer with a fast clock could
    overwrite it and have us install their key where the owner's belonged."""
    acc = _acc()
    acc.keys[B]["pubkey"] = ""
    ok, msg = reconcile.apply_edge(acc, (B, C, "root"),
                                   __import__("fleet.ssh.cmd", fromlist=["Endpoint"])
                                   .Endpoint(target="nowhere.invalid"), install=True)
    assert ok is False
    assert "no pinned key" in msg and "fleet sync" in msg


def test_the_ledger_survives_a_round_trip(tmp_path):
    path = tmp_path / "ledger.yaml"
    led = {f"{A}>{B}>root": EdgeState(desired="absent", observed="present", attempts=3,
                                      last_error="timeout")}
    reconcile.save_ledger(led, path)
    back = reconcile.load_ledger(path)
    assert back[f"{A}>{B}>root"].attempts == 3
    assert back[f"{A}>{B}>root"].last_error == "timeout"


def test_a_missing_ledger_is_not_an_error(tmp_path):
    """Unlike the access list. Nothing observed yet is true on a fresh center, and is
    not a dangerous thing to believe."""
    assert reconcile.load_ledger(tmp_path / "nope.yaml") == {}


def test_reconcile_never_multiplexes():
    """ControlMaster paths are hashed per host, so an edit would share a master with the
    probe fan-out -- and a probe timeout kills the whole process group."""
    import inspect

    src = inspect.getsource(reconcile._remote)
    assert "multiplex=False" in src
