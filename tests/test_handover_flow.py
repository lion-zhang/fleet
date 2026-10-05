"""The handover across machines, which could not complete before a real fleet tried it.

Found on a real run: the signed record stayed on the outgoing center's disk, the
successor never had the access list `--accept` loads, and no member knew what a handover
was -- so an accepted successor would have been refused everywhere. What is asserted:

* A member walks from the key it pinned to the new one only along records signed by the
  key each hands *from*. A forged record leads nowhere.
* The successor stores a handover only if the center it trusts signed it and it names
  this machine.
* The outgoing center steps down only on proof: a reply signed by the successor that
  carries the record this center signed.
* Grants go to each machine as the user it is reached as, and never to the outgoing
  center, which has no route by design.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml

import fleet.config
from fleet.models import Device, Kind
from fleet.ops import handover
from fleet.ops.errors import FleetError
from fleet.state import access as acl
from fleet.state import inventory as inv


def _key(where, name):
    k = where / name
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(k)], check=True)
    return k, k.with_suffix(".pub").read_text().strip()


@pytest.fixture
def keys(tmp_path):
    return {n: _key(tmp_path, n) for n in ("old", "new", "newer", "evil")}


def _record(from_fp, to_pub, key, fleet_id="f1"):
    record = yaml.safe_dump({"kind": "fleet-handover", "fleet_id": fleet_id,
                             "from": from_fp, "to": acl.fingerprint(to_pub),
                             "to_pubkey": to_pub, "at": 1}, sort_keys=False)
    return {"record": record, "signature": acl.sign(record, key)}


# ------------------------------------------------------------------- the chain

def test_a_member_walks_a_signed_handover(keys):
    (ok, opub), (nk, npub) = keys["old"], keys["new"]
    chain = [_record(acl.fingerprint(opub), npub, ok)]
    assert acl.follow_chain(chain, opub) == npub


def test_and_two(keys):
    (ok, opub), (nk, npub), (_, n2pub) = keys["old"], keys["new"], keys["newer"]
    chain = [_record(acl.fingerprint(opub), npub, ok),
             _record(acl.fingerprint(npub), n2pub, nk)]
    assert acl.follow_chain(chain, opub) == n2pub


def test_a_record_not_signed_by_the_key_it_hands_from_leads_nowhere(keys):
    (_, opub), (ek, epub) = keys["old"], keys["evil"]
    forged = [_record(acl.fingerprint(opub), epub, ek)]      # evil signing as old
    assert acl.follow_chain(forged, opub) == opub


def test_a_record_from_some_other_key_is_skipped(keys):
    (_, opub), (nk, npub), (_, epub) = keys["old"], keys["new"], keys["evil"]
    assert acl.follow_chain([_record(acl.fingerprint(npub), epub, nk)], opub) == opub


def test_an_envelope_from_the_new_center_is_accepted_through_the_chain(keys, monkeypatch):
    (ok, opub), (nk, npub) = keys["old"], keys["new"]
    chain = [_record(acl.fingerprint(opub), npub, ok)]
    acl.save_handover_chain(chain)
    monkeypatch.setattr(fleet.config, "FLEET_KEY", nk)
    env = acl.seal(inv.dumps([]), fleet_id="f1")
    note, signer = acl.unseal_trusting(env, opub)
    assert signer == npub and note["fleet_id"] == "f1"


def test_without_the_chain_the_new_center_is_a_stranger(keys):
    (_, opub), (nk, _) = keys["old"], keys["new"]
    env = acl.seal(inv.dumps([]), key_path=nk)
    with pytest.raises(acl.AccessError):
        acl.unseal_trusting(env, opub)


# ------------------------------------------------------------- the successor's side

def _bundle(keys, *, signer="old", to="new"):
    (ok, opub), (_, tpub) = keys["old"], keys[to]
    acc = acl.Access(fleet_id="f1", center=acl.fingerprint(opub), keys={
        acl.fingerprint(opub): {"name": "hub", "pubkey": opub},
        acl.fingerprint(tpub): {"name": "worker", "pubkey": tpub}})
    record = acl.handover_record(acc, acl.fingerprint(tpub))
    body = yaml.safe_dump({"record": record, "signature": acl.sign(record, ok), "chain": [],
                           "access": acl.dumps(acc), "ledger": ""}, sort_keys=False)
    return yaml.safe_dump({"kind": handover.BUNDLE_KIND, "body": body,
                           "signature": acl.sign(body, keys[signer][0])})


@pytest.fixture
def successor(keys, monkeypatch):
    nk, npub = keys["new"]
    monkeypatch.setattr(handover, "ensure_keypair", lambda *a, **k: (nk, npub))
    acl.pin_center_pubkey(keys["old"][1])
    return keys


def test_the_successor_stores_a_handover_its_center_signed(successor):
    assert handover.receive(_bundle(successor)) == "f1"
    assert acl.load().center == acl.fingerprint(successor["old"][1])
    assert "fleet-handover" in acl.INBOX_PATH.read_text()


def test_a_handover_signed_by_anyone_else_is_refused(successor):
    with pytest.raises(FleetError, match="not signed by the center"):
        handover.receive(_bundle(successor, signer="evil"))
    assert not acl.ACCESS_PATH.exists()


def test_a_handover_naming_another_machine_is_refused(successor):
    with pytest.raises(FleetError, match="different machine"):
        handover.receive(_bundle(successor, to="newer"))
    assert not acl.ACCESS_PATH.exists()


def test_accepting_skips_the_outgoing_center_and_keeps_the_chain(successor, monkeypatch):
    from fleet import reconcile as rec
    from fleet.ops import sweep

    handover.receive(_bundle(successor))
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:worker", name="worker", kind=Kind.PERMANENT,
                     endpoints=[{"target": "w", "user": "root", "port": 22}])])
    acc = acl.load()
    dialled = []
    monkeypatch.setattr(rec, "_remote", lambda ep, *a, **k: dialled.append(ep.target) or (True, ""))
    monkeypatch.setattr(sweep, "run", lambda devices: None)
    handover.accept(acc)
    assert dialled == [], "the outgoing center has no route, and is not asked to have one"
    assert acl.load().center == acl.fingerprint(successor["new"][1])
    assert len(acl.handover_chain()) == 1
    assert not acl.INBOX_PATH.exists()
    assert acl.trusted_center_pubkey() == "", "a center pins no center"


def test_accept_with_no_handover_says_where_to_start(successor):
    with pytest.raises(FleetError, match="no handover"):
        handover.accept(None)


# ------------------------------------------------------------ the outgoing center

@pytest.fixture
def outgoing(keys, monkeypatch):
    ok, opub = keys["old"]
    nk, npub = keys["new"]
    monkeypatch.setattr(fleet.config, "FLEET_KEY", ok)
    monkeypatch.setattr(handover, "ensure_keypair", lambda *a, **k: (ok, opub))
    acc = acl.bootstrap("hub", opub, "id:hub")
    acl.enroll(acc, "worker", npub, "id:worker", user="root")
    acl.save(acc)
    acl.HANDING_PATH.write_text(yaml.safe_dump({
        "to": acl.fingerprint(npub), "to_name": "worker", "to_pubkey": npub,
        "url": "http://worker:7373/sync"}))
    return acc, keys


def _reply_from_the_successor(keys, *, with_chain):
    (ok, opub), (nk, npub) = keys["old"], keys["new"]
    env = yaml.safe_load(acl.seal(inv.dumps([]), key_path=nk, fleet_id="f1",
                                  center_url="http://worker:7373/sync"))
    if with_chain:
        env["handovers"] = [_record(acl.fingerprint(opub), npub, ok)]
    return yaml.safe_dump(env, sort_keys=False)


def test_the_outgoing_center_steps_down_on_proof(outgoing, monkeypatch):
    from fleet.ops import sync

    acc, keys = outgoing
    monkeypatch.setattr(sync, "post", lambda *a, **k: _reply_from_the_successor(keys, with_chain=True))
    assert handover.settle(acc) is True
    assert not acl.ACCESS_PATH.exists()
    assert acl.trusted_center_pubkey() == keys["new"][1]
    assert acl.center_url() == "http://worker:7373/sync"


def test_an_answer_without_the_record_is_not_proof(outgoing, monkeypatch):
    """It answered, but has not taken the role -- this machine stays the center."""
    from fleet.ops import sync

    acc, keys = outgoing
    monkeypatch.setattr(sync, "post", lambda *a, **k: _reply_from_the_successor(keys, with_chain=False))
    assert handover.settle(acc) is False
    assert acl.ACCESS_PATH.exists()


def test_no_answer_changes_nothing(outgoing, monkeypatch):
    from fleet.ops import sync

    acc, _ = outgoing
    monkeypatch.setattr(sync, "post", lambda *a, **k: None)
    assert handover.settle(acc) is False
    assert acl.is_center()


def test_grants_go_as_each_machines_user_and_never_to_the_outgoing_center(outgoing, monkeypatch):
    from fleet.ops import sweep

    acc, keys = outgoing
    acl.enroll(acc, "nas", keys["newer"][1], "id:nas", user="alice")
    acl.save(acc)
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:worker", name="worker", kind=Kind.PERMANENT,
                     endpoints=[{"target": "w", "user": "root", "port": 22}]),
              Device(id="id:nas", name="nas", kind=Kind.PERMANENT,
                     endpoints=[{"target": "n", "user": "alice", "port": 22}])])
    monkeypatch.setattr(sweep, "apply_now", lambda *a, **k: None)
    monkeypatch.setattr(handover, "sshrun", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    handover.give_away(acl.load(), "worker", force=True)
    edges = {(e.dst, e.user) for e in acl.load().allow}
    assert edges == {(acl.fingerprint(keys["newer"][1]), "alice")}


def test_a_handover_that_cannot_be_delivered_retires_nothing(outgoing, monkeypatch):
    from fleet.ops import sweep

    acc, _ = outgoing
    acl.HANDING_PATH.unlink()
    inv.save([Device(id="id:worker", name="worker", kind=Kind.PERMANENT,
                     endpoints=[{"target": "w", "user": "root", "port": 22}])])
    monkeypatch.setattr(sweep, "apply_now", lambda *a, **k: None)
    monkeypatch.setattr(handover, "sshrun", lambda *a, **k: subprocess.CompletedProcess(
        a, 127, b"", b"fleet: command not found"))
    with pytest.raises(FleetError, match="not delivered"):
        handover.give_away(acl.load(), "worker", force=True)
    assert not acl.HANDING_PATH.exists()
    assert acl.is_center()


# ---------------------------------------------- found on the second from-scratch run

def test_the_handover_carries_the_inventory(successor):
    """The successor swept from its own older copy: it tried to enrol a machine removed
    since, and named renamed machines by their old names."""
    (ok, opub), (_, tpub) = successor["old"], successor["new"]
    acc = acl.Access(fleet_id="f1", center=acl.fingerprint(opub), keys={
        acl.fingerprint(opub): {"name": "hub", "pubkey": opub},
        acl.fingerprint(tpub): {"name": "builder", "pubkey": tpub}})
    record = acl.handover_record(acc, acl.fingerprint(tpub))
    body = yaml.safe_dump({"record": record, "signature": acl.sign(record, ok), "chain": [],
                           "access": acl.dumps(acc), "ledger": "",
                           "inventory": inv.dumps([Device(id="id:w", name="builder")])})
    inv.save([Device(id="id:w", name="worker", updated_at=1)])
    handover.receive(yaml.safe_dump({"kind": handover.BUNDLE_KIND, "body": body,
                                     "signature": acl.sign(body, ok)}))
    assert [d.name for d in inv.live(inv.load())] == ["builder"]


def test_accepting_retires_the_old_key_even_where_no_ledger_recorded_it(successor, monkeypatch):
    """A machine that joined on an invite placed the center's key itself, so no ledger
    held it -- and the old key stayed on those machines, the new center among them."""
    from fleet import reconcile as rec
    from fleet.ops import sweep

    handover.receive(_bundle(successor))
    acc = acl.load()
    old_fp = acc.center
    acc.keys["SHA256:gpu"] = {"name": "gpu", "pubkey": "ssh-ed25519 AAAA g",
                              "device_id": "id:gpu", "user": "root"}
    acl.save(acc)
    inv.save([Device(id="id:gpu", name="gpu", kind=Kind.PERMANENT,
                     endpoints=[{"target": "g", "user": "root", "port": 22}])])
    monkeypatch.setattr(rec, "_remote", lambda *a, **k: (True, ""))
    monkeypatch.setattr(sweep, "run", lambda devices: None)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: ran.append(k.get("input", b"")) or
                        subprocess.CompletedProcess(argv, 0, b"", b""))
    handover.accept(acl.load())
    st = rec.load_ledger()[f"{old_fp}>SHA256:gpu>root"]
    assert st.desired == "absent" and st.dst_device == "id:gpu"
    assert any(old_fp.encode() in r for r in ran), "and taken off this machine's own file"


def test_the_center_edits_its_own_authorized_keys_in_place(tmp_path, monkeypatch):
    """Over ssh it meant the center dialling itself, which fails."""
    from fleet import reconcile as rec
    from fleet.ssh.cmd import Endpoint

    monkeypatch.setattr(acl, "is_center", lambda *a, **k: True)
    monkeypatch.setattr(rec, "_remote", lambda *a, **k: pytest.fail("dialled itself"))
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: ran.append(argv) or
                        subprocess.CompletedProcess(argv, 0, b"", b""))
    acc = acl.Access(fleet_id="f1", center="SHA256:me", keys={
        "SHA256:me": {"name": "me"}, "SHA256:old": {"name": "old", "pubkey": "ssh-ed25519 A"}})
    ok, _ = rec.apply_edge(acc, ("SHA256:old", "SHA256:me", "root"),
                           Endpoint(target="me"), install=False)
    assert ok and ran


def test_mid_handover_the_old_center_refuses_changes(outgoing, monkeypatch):
    """Found on a real run: with no word from its successor it went on issuing invites,
    which the successor's copy of the list would never have."""
    from typer.testing import CliRunner

    from fleet import cli
    from fleet.ops import sync

    monkeypatch.setattr(sync, "post", lambda *a, **k: None)
    r = CliRunner().invoke(cli.app, ["invite"])
    assert r.exit_code == 2 and "being handed to worker" in r.output
    r = CliRunner().invoke(cli.app, ["center", "--cancel"])
    assert r.exit_code == 0 and not acl.HANDING_PATH.exists()
    monkeypatch.setattr(cli, "_listening", lambda url: True)
    assert CliRunner().invoke(cli.app, ["invite", "--json"]).exit_code == 0



def test_a_retired_center_with_no_route_leaves_no_edge_pending(successor, monkeypatch):
    """Its record has no address by design, so an edge to it read "pending" for ever."""
    from fleet.ops import sweep

    handover.receive(_bundle(successor))
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT)])
    monkeypatch.setattr(sweep, "run", lambda devices: None)
    monkeypatch.setattr(subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, b"", b""))
    old = acl.load().center
    handover.accept(acl.load())
    acc = acl.load()
    assert acc.keys[old].get("no_route")
    assert not any(dst == old for _, dst, _ in acc.edges())
