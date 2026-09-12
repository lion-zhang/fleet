"""The access list: pure data, no network.

Everything dangerous about this design lives in what the list *says*, so the algebra is
tested on its own and the reconciler that acts on it is tested separately.
"""

from __future__ import annotations

import subprocess

import pytest

from fleet import access
from fleet.access import Access, AccessError, Edge

A = "SHA256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "SHA256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
C = "SHA256:ccccccccccccccccccccccccccccccccccccccccccc"


def _acc(center=A):
    return Access(fleet_id="7f3a9c", center=center, keys={
        A: {"name": "macbook", "pubkey": "ssh-ed25519 AAAA a"},
        B: {"name": "lin-xps", "pubkey": "ssh-ed25519 AAAA b"},
        C: {"name": "oracle", "pubkey": "ssh-ed25519 AAAA c"},
    })


def _keypair(tmp_path, name="k"):
    path = tmp_path / name
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(path)],
                   check=True)
    return path, path.with_suffix(".pub").read_text()


# ------------------------------------------------------------------- fingerprints

def test_our_fingerprint_is_the_one_openssh_prints(tmp_path):
    """It has to be, or a human comparing it against `ssh-keygen -lf` sees a mismatch
    that is not really there."""
    path, pub = _keypair(tmp_path)
    theirs = subprocess.run(["ssh-keygen", "-lf", str(path.with_suffix(".pub"))],
                            capture_output=True, text=True).stdout.split()[1]
    assert access.fingerprint(pub) == theirs


def test_rubbish_is_rejected_rather_than_hashed():
    for bad in ("", "hello", "ssh-ed25519 not-base64!!"):
        with pytest.raises(AccessError):
            access.fingerprint(bad)


# ------------------------------------------------------------------------- edges

def test_the_center_reaches_everything_without_it_being_written():
    """Computed, not stored: a new center would otherwise mean rewriting every row, and
    a hand-edited file could omit the one edge that makes the fleet manageable."""
    acc = _acc()
    assert not acc.allow
    assert acc.edges() == {(A, B, "root"), (A, C, "root")}


def test_a_grant_adds_exactly_one_edge():
    acc = _acc()
    assert access.grant(acc, B, C) is True
    assert access.grant(acc, B, C) is False, "already there"
    assert (B, C, "root") in acc.edges()


def test_the_user_is_part_of_the_edge():
    """A box answers as both root@ and ubuntu@, so "remove B's key from C" is ambiguous
    without saying whose authorized_keys."""
    acc = _acc()
    access.grant(acc, B, C, user="root")
    access.grant(acc, B, C, user="ubuntu")
    assert len(acc.allow) == 2
    access.revoke(acc, B, C, user="root")
    assert [e.user for e in acc.allow] == ["ubuntu"]


def test_the_centers_own_access_cannot_be_revoked():
    """It would leave a machine no center could ever write to again, and there is no
    recovery path from that. Not expressible, rather than discouraged."""
    acc = _acc()
    with pytest.raises(AccessError, match="center"):
        access.revoke(acc, A, B)


def test_absence_is_denial():
    acc = _acc()
    assert (B, C, "root") not in acc.edges()


# -------------------------------------------------------------------- loading

def test_a_missing_list_raises_rather_than_reading_as_empty(tmp_path):
    """The whole point. An empty list means "remove every edge", so degrading to it
    would strip the center's own key from every machine in one sweep."""
    with pytest.raises(AccessError, match="not a center"):
        access.load(tmp_path / "nope.yaml")


def test_an_unreadable_list_raises(tmp_path):
    p = tmp_path / "access.yaml"
    p.write_text("allow: [this is not\n  valid: yaml")
    with pytest.raises(AccessError):
        access.load(p)
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(AccessError, match="mapping"):
        access.load(p)


def test_a_list_round_trips(tmp_path):
    acc = _acc()
    access.grant(acc, B, C, note="training")
    p = tmp_path / "access.yaml"
    access.save(acc, p)
    back = access.load(p)
    assert back.center == A and back.fleet_id == "7f3a9c"
    assert back.edges() == acc.edges()
    assert back.allow[0].note == "training"


def test_saving_bumps_the_generation(tmp_path):
    acc = _acc()
    p = tmp_path / "access.yaml"
    access.save(acc, p)
    access.save(acc, p)
    assert access.load(p).generation == 2


# -------------------------------------------------------------------- resolving

def test_a_name_resolves_but_only_exactly():
    """`inventory.find` takes a unique prefix, which is a fine convenience for `fleet
    ssh` and a poor one for a command that edits who can reach what."""
    acc = _acc()
    assert access.resolve(acc, "oracle") == C
    assert access.resolve(acc, C) == C
    with pytest.raises(AccessError):
        access.resolve(acc, "orac")


# ------------------------------------------------------------------ am I the center

def test_without_the_private_key_you_are_not_the_center(tmp_path):
    path, pub = _keypair(tmp_path)
    acc = Access(center=access.fingerprint(pub), keys={})
    assert access.is_center(acc, key_path=path) is True
    path.unlink()
    assert access.is_center(acc, key_path=path) is False


def test_a_different_key_is_not_the_center(tmp_path):
    mine, _ = _keypair(tmp_path, "mine")
    _, theirs_pub = _keypair(tmp_path, "theirs")
    acc = Access(center=access.fingerprint(theirs_pub), keys={})
    assert access.is_center(acc, key_path=mine) is False


def test_a_list_with_no_center_names_nobody(tmp_path):
    path, _ = _keypair(tmp_path)
    assert access.is_center(Access(), key_path=path) is False


# ---------------------------------------------------------------------- signing

def test_a_signature_round_trips(tmp_path):
    path, pub = _keypair(tmp_path)
    payload = access.dumps(_acc())
    assert access.verify(payload, access.sign(payload, path), pub) is True


def test_a_forged_list_does_not_verify(tmp_path):
    """The attack this exists for: any granted peer can reach a spoke and run the sync
    filter there, so ssh proves *a* peer, not *the* center."""
    path, pub = _keypair(tmp_path)
    good = access.dumps(_acc())
    sig = access.sign(good, path)
    forged = access.dumps(_acc(center=B))
    assert access.verify(forged, sig, pub) is False


def test_a_signature_from_another_key_does_not_verify(tmp_path):
    mine, _ = _keypair(tmp_path, "mine")
    _, centers_pub = _keypair(tmp_path, "center")
    payload = access.dumps(_acc())
    assert access.verify(payload, access.sign(payload, mine), centers_pub) is False


def test_an_empty_signature_is_not_a_pass(tmp_path):
    _, pub = _keypair(tmp_path)
    assert access.verify("anything", "", pub) is False
