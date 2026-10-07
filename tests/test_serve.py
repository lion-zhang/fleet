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
    return key, key.with_suffix(".pub").read_text(encoding="utf-8").strip()


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


def _post(url, body: bytes, headers=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_a_stranger_is_refused_before_its_body_is_read(a_center, monkeypatch):
    """Found in the final audit: the body was parsed before anyone was known, and parsing
    is where the cost is. A signer the fleet has not pinned is now turned away on the
    header alone."""
    from fleet.state import untrusted

    monkeypatch.setattr(untrusted, "load", lambda *a: pytest.fail("parsed a stranger's body"))
    httpd, url = serve.serve_in_thread()
    try:
        code, _ = _post(url, b"x" * 1000, {serve.SIGNER_HEADER: "SHA256:nobody"})
        assert code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_member_says_who_it_is_and_is_answered(a_center):
    fp = acl.fingerprint(acl.claimed_signer(_sealed(a_center["skey"])))
    httpd, url = serve.serve_in_thread()
    try:
        code, body = _post(url, _sealed(a_center["skey"]).encode(), {serve.SIGNER_HEADER: fp})
        assert code == 200, body
        assert "hub" in acl.unseal(body, a_center["cpub"])["inventory"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_yaml_aliases_from_the_network_are_refused():
    """A few hundred bytes of nested aliases expand ninefold per level."""
    import yaml

    from fleet.state import untrusted

    bomb = "a0: &a0 [x, x]\na1: &a1 [*a0, *a0]\nk: *a1\n"
    with pytest.raises(yaml.YAMLError):
        untrusted.load(bomb)
    with pytest.raises(acl.AccessError):
        acl.unseal("body: *a\n", "ssh-ed25519 AAAA")
    assert untrusted.load(untrusted.dump({"x": [1, 2], "y": {"z": "w"}})) == {
        "x": [1, 2], "y": {"z": "w"}}


def test_a_busy_center_says_so_rather_than_queueing(a_center, monkeypatch):
    import threading

    sem = threading.BoundedSemaphore(1)
    sem.acquire()                                   # every slot taken
    monkeypatch.setattr(serve, "_IN_FLIGHT", sem)
    httpd, url = serve.serve_in_thread()
    try:
        code, _ = _post(url, _sealed(a_center["skey"]).encode())
        assert code == 503
    finally:
        sem.release()
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


def test_a_grant_with_json_prints_one_document(a_center, monkeypatch):
    """An MCP client parses stdout; the progress lines in front of the JSON made every
    grant it asked for come back unreadable."""
    import json

    from typer.testing import CliRunner

    from fleet import cli
    from fleet import reconcile as rec

    inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
              Device(id="id:box", name="box", kind=Kind.PERMANENT,
                     endpoints=[{"target": "1.2.3.4", "user": "root", "port": 22}])],
             inv.INVENTORY_PATH)
    monkeypatch.setattr(rec, "apply_edge", lambda acc, edge, ep, **kw: (True, ""))
    r = CliRunner().invoke(cli.app, ["access", "box", "--allow", "hub",
                                     "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["change"] == "granted" and doc["changed"] is True and doc["to"] == "box"
    assert "applied on box" in r.stderr


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


def test_a_member_cannot_delete_another_machine_by_syncing(a_center):
    """Found on a real fleet: `fleet rm nas` on a member tombstoned nas locally, and the
    next sync deleted it from the center -- keys still installed, record gone."""
    from fleet.models import Device, Kind

    gone = Device(id="id:hub", name="hub", kind=Kind.PERMANENT, deleted_at=2_000_000_000,
                  updated_at=2_000_000_000)
    code, _ = serve.exchange(_sealed(a_center["skey"], [gone]))
    assert code == 200
    assert "hub" in {d.name for d in inv.live(inv.load(inv.INVENTORY_PATH))}


def test_a_member_may_remove_itself(a_center):
    from fleet.models import Device, Kind

    me = Device(id="id:box", name="box", kind=Kind.PERMANENT, deleted_at=2_000_000_000,
                updated_at=2_000_000_000)
    serve.exchange(_sealed(a_center["skey"], [me]))
    assert "box" not in {d.name for d in inv.live(inv.load(inv.INVENTORY_PATH))}


def test_the_listener_binds_without_a_reverse_name_lookup(monkeypatch):
    """HTTPServer.server_bind calls socket.getfqdn before listening; on a real macOS
    runner that hung for over a minute, so the launchd service ran with its port shut."""
    import socket

    from fleet import serve

    def hang(*a, **k):
        raise AssertionError("getfqdn called: it can hang for minutes on macOS")
    monkeypatch.setattr(socket, "getfqdn", hang)
    httpd = serve.build("127.0.0.1", 0)
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()


def test_a_member_cannot_redirect_or_promote_another_machine(a_center):
    """Found in review: the listener merged a member's whole inventory. A record for
    another machine with a future `updated_at` and an endpoint of the member's choosing
    won outright -- so the center's next sweep "revoked" keys on whatever host answered
    there, the ledger said done, and the signed result sent every machine's `fleet ssh`
    to that host. Labels may change; where a machine is, and who is center, may not."""
    from fleet.models import Device, Kind

    inv.save(inv.load(inv.INVENTORY_PATH) + [Device(
        id="id:nas", name="nas", kind=Kind.PERMANENT, updated_at=1_000,
        endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])], inv.INVENTORY_PATH)
    forged = Device(id="id:nas", name="nas", kind=Kind.PERMANENT, role="center",
                    updated_at=4_000_000_000, tags=["backup"],
                    endpoints=[{"target": "192.0.2.66", "user": "root", "port": 22,
                                "preference": -100}])
    code, _ = serve.exchange(_sealed(a_center["skey"], [forged]))
    assert code == 200
    now = {d.id: d for d in inv.load(inv.INVENTORY_PATH)}
    assert [e["target"] for e in now["id:nas"].endpoints] == ["10.0.0.5"]
    assert now["id:nas"].role != "center" and now["id:hub"].role == "center"
    assert now["id:nas"].tags == ["backup"], "a label is the member's to change"
    assert now["id:nas"].updated_at < 4_000_000_000, "a future clock wins nothing"


def test_a_member_cannot_make_itself_the_center(a_center):
    from fleet.models import Device, Kind

    me = Device(id="id:box", name="box", kind=Kind.PERMANENT, role="center",
                updated_at=4_000_000_000)
    serve.exchange(_sealed(a_center["skey"], [me]))
    now = {d.id: d for d in inv.load(inv.INVENTORY_PATH)}
    assert now["id:box"].role != "center" and now["id:hub"].role == "center"


def test_a_member_cannot_point_its_own_record_at_another_host(a_center):
    """Found in the final audit: a member's own record was taken whole, addresses
    included -- and the first address by preference is where the center dials to place
    keys. A route of preference 0 to another host the center can log into would have
    had the next grant to this member written into that host's authorized_keys."""
    from fleet.models import Device, Kind

    before = {d.id: d for d in inv.load(inv.INVENTORY_PATH)}["id:box"].endpoints
    me = Device(id="id:box", name="box", kind=Kind.PERMANENT, updated_at=4_000_000_000,
                tags=["mine"],
                endpoints=[{"target": "192.0.2.77", "user": "root", "port": 22,
                            "preference": 0}])
    assert serve.exchange(_sealed(a_center["skey"], [me]))[0] == 200
    now = {d.id: d for d in inv.load(inv.INVENTORY_PATH)}["id:box"]
    assert now.endpoints == before
    assert now.tags == ["mine"], "everything else about itself is still its own"


def test_what_the_center_refused_does_not_stay_on_the_member():
    """A member renamed another machine; the center kept only the labels. Stamped equal,
    the member's next pull tied and kept its rejected name for good."""
    from fleet.models import Device, Kind

    center = [Device(id="id:nas", name="nas", kind=Kind.PERMANENT, updated_at=1_000)]
    member = [Device(id="id:nas", name="renamed", kind=Kind.PERMANENT, updated_at=2_000,
                     tags=["x"])]
    sent = [Device(id="id:nas", name="renamed", kind=Kind.PERMANENT, updated_at=2_000,
                   tags=["x"])]
    taken = inv.from_member(center, sent, "id:box")
    merged_center, _ = inv.merge(center, taken)
    assert merged_center[0].name == "nas" and merged_center[0].tags == ["x"]
    on_member, _ = inv.merge_from_center(member, merged_center, sent_at=3_000)
    assert on_member[0].name == "nas", "the member takes the center's record"


def test_a_member_may_still_add_a_machine(a_center):
    """`fleet add` works on a member; the center picks the machine up from its copy."""
    from fleet.models import Device, Kind

    new = Device(id="id:new", name="new", kind=Kind.PERMANENT, role="center",
                 endpoints=[{"target": "10.0.0.9", "user": "root", "port": 22}])
    serve.exchange(_sealed(a_center["skey"], [new]))
    now = {d.id: d for d in inv.load(inv.INVENTORY_PATH)}
    assert "id:new" in now and now["id:new"].role == "none"


def test_the_sweep_takes_from_a_members_reply_only_what_it_may_change():
    """The sweep merges what each member prints back -- unsigned. Same rule."""
    from fleet.models import Device, Kind

    current = [Device(id="id:nas", name="nas", kind=Kind.PERMANENT, updated_at=1_000,
                      endpoints=[{"target": "10.0.0.5", "user": "root", "port": 22}])]
    reply = [Device(id="id:nas", name="renamed", kind=Kind.RENTAL, updated_at=9_000,
                    endpoints=[{"target": "192.0.2.66", "user": "root", "port": 22}],
                    cost={"usd_per_hour": 1.0})]
    taken = inv.from_member(current, reply, sender_id="id:box")
    assert len(taken) == 1
    assert taken[0].name == "nas" and taken[0].kind is Kind.PERMANENT
    assert [e["target"] for e in taken[0].endpoints] == ["10.0.0.5"]
    assert taken[0].cost == {"usd_per_hour": 1.0}


def test_a_client_that_sends_nothing_cannot_hold_the_listener():
    """The handler's socket timeout is what frees a thread from a stalled client."""
    assert 0 < serve._Handler.timeout <= 60


def test_a_relayed_reading_keeps_its_age(tmp_path, monkeypatch):
    """Found in review: relayed rows were stamped with the time they arrived, so a
    machine the center last saw three days ago showed `ok`, 0s old, and was never asked
    again -- every pull re-stamped it."""
    import time

    from fleet.ops import sync
    from fleet.state import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    three_days_ago = int(time.time()) - 3 * 86400
    sync.record_relayed([{"device_id": "id:nas", "status": "timeout",
                          "probed_at": three_days_ago, "snapshot": None,
                          "error_class": "timeout", "error_detail": "no answer"}])
    conn = store.connect()
    try:
        st, _ = store.latest(conn, "id:nas")
    finally:
        conn.close()
    assert st["last_probe_at"] == three_days_ago
    assert not store.is_fresh(st, 60)
    assert st["error_class"] == "timeout" and st["error_detail"] == "no answer"


def test_a_member_shows_the_centers_reading_of_a_machine_it_cannot_reach(tmp_path, monkeypatch):
    """fleet needs the center to reach every machine, not members to reach each other. A
    member with no route or no key to a machine showed it down while the center had
    just measured it fine. Not on the center, whose own failure is what matters."""
    import time

    from fleet.models import ProbeResult, Snapshot, Status
    from fleet.state import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cache.db")
    conn = store.connect()
    try:
        store.record(conn, "id:nas", ProbeResult(status=Status.AUTH_FAILED), source="self")
        store.record(conn, "id:nas", ProbeResult(status=Status.OK, snapshot=Snapshot(hostname="nas")),
                     source="broadcast", at=int(time.time()) + 1)
        on_member, snap = store.latest(conn, "id:nas", relayed_over_unreachable=True)
        on_center, _ = store.latest(conn, "id:nas")
        assert on_member["source"] == "broadcast" and on_member["status"] == "ok"
        assert snap["hostname"] == "nas"
        assert on_center["source"] == "self" and on_center["status"] == "auth_failed"
        # a machine that answered and failed is still reported as it answered
        store.record(conn, "id:nas", ProbeResult(status=Status.PROBE_ERROR), source="self")
        assert store.latest(conn, "id:nas", relayed_over_unreachable=True)[0]["source"] == "self"
    finally:
        conn.close()
