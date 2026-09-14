"""The center, listening, and machines that sync themselves.

Two properties carry this design and both are asserted here: the listener never accepts
first contact, and a caller it has not pinned is refused before a byte of its payload is
read. Everything else is the same sealed envelope `fleet sync --serve` has always used,
which is why so little new had to be trusted.
"""

from __future__ import annotations

import subprocess

import pytest

from fleet.state import access as acl
from fleet.state import inventory as inv
from fleet import serve
from fleet.state import store
from fleet.models import Device, Kind


def _keypair(tmp_path, name):
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    return key, key.with_suffix(".pub").read_text().strip()


@pytest.fixture
def a_center(tmp_path, monkeypatch):
    """A center with one machine enrolled, plus a stranger's key that is not."""
    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")

    ckey, cpub = _keypair(tmp_path, "center")
    skey, spub = _keypair(tmp_path, "spoke")
    xkey, _xpub = _keypair(tmp_path, "stranger")
    import fleet.config
    monkeypatch.setattr(fleet.config, "FLEET_KEY", ckey)
    monkeypatch.setattr(acl, "FLEET_KEY", ckey, raising=False)

    acc = acl.bootstrap("hub", cpub, "id:hub", path=acl.ACCESS_PATH)
    acl.enroll(acc, "box", spub, "id:box", user="root")
    acl.save(acc, acl.ACCESS_PATH)
    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:box", name="box", kind=Kind.PERMANENT)], inv.INVENTORY_PATH)
    return {"ckey": ckey, "cpub": cpub, "skey": skey, "spub": spub, "xkey": xkey}


def tmp_path_for(a_center) -> "object":
    """The sandbox the fixture built, taken from a path it already owns."""
    return a_center["ckey"].parent


def _sealed(key, devices=None):
    return acl.seal(inv.dumps(devices or []), key_path=key)


# ------------------------------------------------------------------ the gate

def test_a_machine_the_fleet_never_pinned_is_refused(a_center):
    """The whole authenticator: the envelope names a key, and it must be one enrolment
    already put in the access list. A stranger's signature is perfectly valid and still
    means nothing here."""
    code, body = serve.exchange(_sealed(a_center["xkey"]))
    assert code == 403
    assert "does not" in body or "knows" in body


def test_the_listener_never_accepts_first_contact(a_center, monkeypatch):
    """`fleet sync --serve` falls back to trust-on-first-use, which is safe only because
    the center always spoke first -- it pinned whoever it had just dialled. A listener
    reverses who speaks first, so the same fallback would let the earliest caller pin
    itself as the center."""
    called = []
    monkeypatch.setattr(acl, "unseal_first_contact",
                        lambda p: called.append(p) or "")
    code, _ = serve.exchange(_sealed(a_center["xkey"]))
    assert code == 403
    assert not called, "first contact must never be reachable from the listening side"


def test_a_tampered_body_is_refused_even_from_a_pinned_key(a_center):
    payload = _sealed(a_center["skey"])
    tampered = payload.replace("devices: []", "devices: [] # and one more thing")
    code, _ = serve.exchange(tampered)
    assert code in (400, 403)


def test_an_unsigned_payload_is_refused(a_center):
    code, _ = serve.exchange("inventory: {}\n")
    assert code == 403, "no claimed signer at all is not a machine we know"


# ------------------------------------------------------------------ the exchange

def test_a_pinned_machine_gets_a_signed_inventory_back(a_center):
    code, body = serve.exchange(_sealed(a_center["skey"]))
    assert code == 200
    note = acl.unseal(body, a_center["cpub"])
    names = {d.name for d in inv.loads(note["inventory"])}
    assert {"hub", "box"} <= names


def test_what_a_machine_sends_is_merged(a_center):
    fresh = Device(id="id:new", name="newbox", kind=Kind.RENTAL)
    code, _ = serve.exchange(_sealed(a_center["skey"], [fresh]))
    assert code == 200
    assert "newbox" in {d.name for d in inv.live(inv.load(inv.INVENTORY_PATH))}


def test_the_reply_carries_where_to_come_back_to(a_center, monkeypatch):
    """A machine pins the center's key but has never known its address, so it could only
    be told by being dialled. Carrying it inside the signed body means the address
    arrives over a channel the machine already verifies."""
    monkeypatch.setattr(serve, "_URL", {"value": "http://hub.example:7373/sync"})
    code, body = serve.exchange(_sealed(a_center["skey"]))
    assert code == 200
    assert acl.unseal(body, a_center["cpub"])["center_url"] == \
        "http://hub.example:7373/sync"


def test_a_machine_that_is_not_the_center_will_not_serve(a_center, monkeypatch):
    """Handing out a list we cannot sign would produce one nobody accepts anyway."""
    monkeypatch.setattr(acl, "is_center", lambda acc: False)
    code, body = serve.exchange(_sealed(a_center["skey"]))
    assert code == 503 and "center" in body


# ------------------------------------------------------------------ over a socket

def test_end_to_end_over_http(a_center):
    import urllib.request

    httpd, url = serve.serve_in_thread()
    try:
        req = urllib.request.Request(url, data=_sealed(a_center["skey"]).encode(),
                                     method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
        assert resp.status == 200
        assert "hub" in acl.unseal(body, a_center["cpub"])["inventory"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_health_says_nothing_about_the_fleet():
    """An unauthenticated caller learns a center is here, which the open port already
    told them, and nothing else."""
    import urllib.request

    httpd, url = serve.serve_in_thread()
    try:
        with urllib.request.urlopen(url.replace("/sync", "/health"), timeout=10) as resp:
            body = resp.read().decode()
        assert "fleet" in body
        for leak in ("hub", "box", "ssh-ed25519", "pubkey"):
            assert leak not in body
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ the spoke's side

@pytest.fixture
def a_spoke(tmp_path, monkeypatch):
    """A machine that has been enrolled: it knows the center's key and its address."""
    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(inv, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    key, pub = _keypair(tmp_path, "me")
    import fleet.config
    monkeypatch.setattr(fleet.config, "FLEET_KEY", key)
    inv.save([Device(id="id:me", name="me", kind=Kind.PERMANENT)], inv.INVENTORY_PATH)
    acl.pin_center_pubkey("ssh-ed25519 AAAA center", acl.CACHE_PATH)
    acl.note_center_url("http://hub.example/sync", acl.CACHE_PATH)
    return key


def test_a_fresh_copy_is_not_refetched(a_spoke, monkeypatch):
    """Lazy means lazy: inside the TTL a read costs nothing at all."""
    from fleet.ops import sync

    acl.note_center_seen(acl.CACHE_PATH)
    monkeypatch.setattr(sync, "post", lambda *a, **k: pytest.fail("should not have asked"))
    sync.ensure_fresh()


def test_a_stale_copy_is_refetched(a_spoke, monkeypatch):
    from fleet.ops import sync

    asked = []
    monkeypatch.setattr(sync, "post", lambda url, payload, **k: asked.append(url) or None)
    sync.ensure_fresh()                       # never synced: seen_at is 0
    assert asked == ["http://hub.example/sync"]


def test_a_center_that_is_down_costs_freshness_and_nothing_else(a_spoke, monkeypatch):
    """Every command that refreshes already works from local state, and the design's own
    rule is that a sync outage must not become a fleet outage."""
    from fleet.ops import sync

    monkeypatch.setattr(sync, "post", lambda *a, **k: None)
    sync.ensure_fresh()                       # must not raise
    assert [d.name for d in inv.live(inv.load(inv.INVENTORY_PATH))] == ["me"]


def test_a_reply_not_signed_by_the_pinned_center_is_ignored(a_spoke, monkeypatch, tmp_path):
    """The machine pinned a key at enrolment. Anything else answering on that address is
    a stranger, however well-formed its envelope."""
    from fleet.ops import sync

    other, _ = _keypair(tmp_path, "impostor")
    forged = acl.seal(inv.dumps([Device(id="id:evil", name="evil", kind=Kind.PERMANENT)]),
                      key_path=other)
    monkeypatch.setattr(sync, "post", lambda *a, **k: forged)
    sync.ensure_fresh()
    assert "evil" not in {d.name for d in inv.live(inv.load(inv.INVENTORY_PATH))}


def test_a_machine_that_was_never_told_where_to_look_stays_quiet(tmp_path, monkeypatch):
    from fleet.ops import sync

    for n in ("ACCESS_PATH", "LEDGER_PATH", "CACHE_PATH", "OUTBOX_PATH"):
        monkeypatch.setattr(acl, n, tmp_path / getattr(acl, n).name)
    monkeypatch.setattr(sync, "post", lambda *a, **k: pytest.fail("nowhere to ask"))
    sync.ensure_fresh()


# ------------------------------------------------------------------ grants apply themselves

def test_a_grant_is_applied_on_the_spot(a_center, monkeypatch):
    """Telling someone to run a second command to mean what they just said was always a
    poor trade."""
    from typer.testing import CliRunner

    from fleet import cli
    from fleet import reconcile as rec

    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])],
             inv.INVENTORY_PATH)
    applied = []
    monkeypatch.setattr(rec, "apply_edge",
                        lambda acc, edge, ep, **kw: applied.append((edge, kw)) or (True, ""))

    r = CliRunner().invoke(cli.app, ["access", "box", "--allow", "hub"])
    assert r.exit_code == 0, r.output
    assert applied and applied[0][1]["install"] is True
    assert "applied on box" in r.output


def test_a_revoke_reaches_the_target_without_waiting_to_be_asked(a_center, monkeypatch):
    """Lazy pull cannot carry a removal: a machine that waits to be asked would keep the
    key until it next happened to sync, which for an idle machine is never -- while the
    peer losing access carries on using it."""
    from typer.testing import CliRunner

    from fleet import cli
    from fleet import reconcile as rec

    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])],
             inv.INVENTORY_PATH)
    # a peer, not the center: revoking the center's own access is refused outright,
    # because it would strand the machine with no way back
    _, opub = _keypair(tmp_path_for(a_center), "peer")
    acc = acl.load(acl.ACCESS_PATH)
    acl.enroll(acc, "peer", opub, "id:peer", user="root")
    acl.grant(acc, acl.resolve(acc, "peer"), acl.resolve(acc, "box"), user="root")
    acl.save(acc, acl.ACCESS_PATH)

    applied = []
    monkeypatch.setattr(rec, "apply_edge",
                        lambda a, edge, ep, **kw: applied.append(kw) or (True, ""))
    r = CliRunner().invoke(cli.app, ["access", "box", "--deny", "peer"])
    assert r.exit_code == 0, r.output
    assert applied and applied[0]["install"] is False


def test_an_unreachable_target_stays_pending_rather_than_failing(a_center, monkeypatch):
    """The honest report, and the one `fleet access` already shows."""
    from typer.testing import CliRunner

    from fleet import cli
    from fleet import reconcile as rec

    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])],
             inv.INVENTORY_PATH)
    monkeypatch.setattr(rec, "apply_edge", lambda *a, **k: (False, "connection refused"))

    r = CliRunner().invoke(cli.app, ["access", "box", "--allow", "hub"])
    assert r.exit_code == 0, "a machine being off is not a failure of the grant"
    assert "not reached" in r.output and "pending" in r.output
