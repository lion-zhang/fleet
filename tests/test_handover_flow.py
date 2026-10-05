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
