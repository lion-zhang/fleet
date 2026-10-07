"""What counts as one public key. Everything pinned, and everything written into another
machine's authorized_keys, goes through `canonical_pubkey` first."""

from __future__ import annotations

import pytest

from fleet.state import access as acl

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq2d9cVfY0f8Q2l fleet:box"


def test_one_key_is_kept_as_it_is():
    assert acl.canonical_pubkey(KEY + "\n") == KEY


@pytest.mark.parametrize("bad", [
    KEY + "\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5OTHER other",     # a second key
    KEY + "\r# fleet:7f3a9c:end from=x",                       # a forged marker
    "ssh-ed25519\nAAAAC3NzaC1lZDI1NTE5",                       # split across lines
    "not-a-type AAAAC3NzaC1lZDI1NTE5",                         # unknown key type
    "ssh-ed25519 AAAA$(touch x)",                              # not base64
    "",
])
def test_anything_but_one_key_is_refused(bad):
    with pytest.raises(acl.AccessError):
        acl.canonical_pubkey(bad)
    with pytest.raises(acl.AccessError):
        acl.fingerprint(bad)


def test_an_odd_comment_is_dropped_not_written():
    assert acl.canonical_pubkey("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 a b'c;d") == \
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5"


def test_enrolling_a_smuggled_key_fails_and_pins_nothing():
    acc = acl.Access(fleet_id="7f3a9c")
    with pytest.raises(acl.AccessError):
        acl.enroll(acc, "box", KEY + "\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5ZZ x", "id:box")
    assert acc.keys == {}
    fp = acl.enroll(acc, "box", KEY, "id:box")
    assert acc.keys[fp]["pubkey"] == KEY
