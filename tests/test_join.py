"""Joining on an invite: the machine dials the center, holding a code the center issued.

What carries the design, and is asserted here:

* An invite admits exactly one key, once. A second machine is refused; the same machine
  asking again (a reply lost on the way back) is not.
* The secret never travels. A request proves it holds the invite with a MAC over its own
  signed envelope, so a MAC lifted off one request is worthless on another.
* The joiner trusts only the center the code names. No trust on first use: a reply
  signed by any other key pins nothing and installs nothing.
* Nothing is spent on a join that fails, and nothing is saved on one that is refused.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager

import pytest

import fleet.config
from fleet.models import Device, Kind
from fleet.ops import join as join_mod
from fleet.ops.errors import FleetError
from fleet.state import access as acl
from fleet.state import inventory as inv
from fleet.state import invites
from fleet.state import store


def _keypair(where, name):
    key = where / name
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    return key, key.with_suffix(".pub").read_text(encoding="utf-8").strip()


def _paths(root):
    return {
        (acl, "ACCESS_PATH"): root / "access.yaml",
        (acl, "LEDGER_PATH"): root / "access-ledger.yaml",
        (acl, "CACHE_PATH"): root / "access-cache.yaml",
        (acl, "OUTBOX_PATH"): root / "access-outbox.yaml",
        (inv, "INVENTORY_PATH"): root / "inventory.yaml",
        (store, "DB_PATH"): root / "cache.db",
        (invites, "INVITES_PATH"): root / "access-invites.yaml",
        (fleet.config, "FLEET_KEY"): root / "id_ed25519",
    }


@contextmanager
def being(root):
    """Be the machine whose state lives under `root`, for the duration."""
    saved = {k: getattr(*k) for k in _paths(root)}
    for (mod, attr), value in _paths(root).items():
        setattr(mod, attr, value)
    try:
        yield
    finally:
        for (mod, attr), value in saved.items():
            setattr(mod, attr, value)


@pytest.fixture
def fleet_of_two(tmp_path, monkeypatch):
    """A center under c/, a machine that has never heard of it under j/."""
    c, j = tmp_path / "c", tmp_path / "j"
    c.mkdir()
    j.mkdir()
    ckey, cpub = _keypair(c, "id_ed25519")
    jkey, jpub = _keypair(j, "id_ed25519")
    box_key, box_pub = _keypair(tmp_path, "box")
    with being(c):
        acc = acl.bootstrap("hub", cpub, "id:hub")
        acl.enroll(acc, "box", box_pub, "id:box", user="root")
        acl.save(acc)
        inv.save([Device(id="id:hub", name="hub", kind=Kind.PERMANENT, role="center"),
                  Device(id="id:box", name="box", kind=Kind.PERMANENT,
                         endpoints=[{"name": "primary", "target": "box.example",
                                     "user": "root", "port": 22}])])
    # Start as the joiner: that is the side most tests act from.
    for (mod, attr), value in _paths(j).items():
        monkeypatch.setattr(mod, attr, value)
    return {"c": c, "j": j, "cpub": cpub, "jpub": jpub, "jkey": jkey,
            "cfp": acc.center, "fleet_id": acc.fleet_id}


def _me(name="newbox", dev_id="linux:machine-id:new", user="alice", target=""):
    return Device(id=dev_id, name=name, kind=Kind.PERMANENT,
                  endpoints=[{"name": "primary", "target": target, "user": user,
                              "port": 22, "preference": 10}])


def _invite(f, **kw):
    with being(f["c"]):
        return invites.create(**kw)


def _ask(f, request, peer="10.0.0.7"):
    with being(f["c"]):
        return join_mod.handle(request, peer=peer, center_url="http://hub.example:7373/sync")


def _request(f, invite, secret, dev=None, key=None):
    return join_mod.build_request(dev or _me(), invite_id=invite.id, secret=secret,
                                  key_path=key or f["jkey"])


# ----------------------------------------------------------------------- the code

def test_a_code_carries_where_who_and_which_invite():
    code = join_mod.encode_code("http://hub:7373/sync", "SHA256:abc", "1a2b3c4d", "s3cret")
    assert code.startswith("fleet1:")
    assert join_mod.decode_code(code) == {"url": "http://hub:7373/sync",
                                          "center": "SHA256:abc", "id": "1a2b3c4d",
                                          "secret": "s3cret"}


@pytest.mark.parametrize("bad", ["", "hello", "fleet1:", "fleet1:!!!!", "fleet1:e30"])
def test_a_damaged_code_is_refused_with_a_reason(bad):
    with pytest.raises(FleetError):
        join_mod.decode_code(bad)


def test_a_code_survives_the_whitespace_a_paste_brings():
    code = join_mod.encode_code("http://hub:7373/sync", "SHA256:abc", "id", "s")
    assert join_mod.decode_code(f"  {code}\n")["id"] == "id"


def test_the_join_address_sits_beside_the_sync_one():
    assert join_mod.join_url("http://hub:7373/sync") == "http://hub:7373/join"
    assert join_mod.join_url("http://hub:7373/sync/") == "http://hub:7373/join"
    assert join_mod.join_url("http://hub:7373") == "http://hub:7373/join"


# ---------------------------------------------------------------- the invite store

def test_the_secret_is_not_written_down(tmp_path):
    path = tmp_path / "invites.yaml"
    _, secret = invites.create(path=path)
    assert secret not in path.read_text(encoding="utf-8")


def test_an_invite_is_good_once_and_for_one_key(tmp_path):
    path = tmp_path / "invites.yaml"
    inv_, secret = invites.create(path=path)
    msg, mac = "body", invites.mac(invites.secret_key(secret), "body")
    invites.check(inv_.id, msg, mac, "SHA256:a", path=path)
    invites.redeem(inv_.id, "SHA256:a", "a", path=path)
    invites.check(inv_.id, msg, mac, "SHA256:a", path=path)     # the same key again
    with pytest.raises(invites.InviteError, match="already been used"):
        invites.check(inv_.id, msg, mac, "SHA256:b", path=path)
    with pytest.raises(invites.InviteError, match="already been used"):
        invites.redeem(inv_.id, "SHA256:b", "b", path=path)


def test_an_expired_invite_is_refused(tmp_path):
    path = tmp_path / "invites.yaml"
    inv_, secret = invites.create(path=path, ttl_s=60)
    mac = invites.mac(invites.secret_key(secret), "m")
    with pytest.raises(invites.InviteError, match="expired"):
        invites.check(inv_.id, "m", mac, "SHA256:a", path=path, now=inv_.expires_at)


def test_a_withdrawn_invite_is_refused(tmp_path):
    path = tmp_path / "invites.yaml"
    inv_, secret = invites.create(path=path)
    assert invites.revoke(inv_.id, path=path).state() == "revoked"
    with pytest.raises(invites.InviteError, match="withdrawn"):
        invites.check(inv_.id, "m", invites.mac(invites.secret_key(secret), "m"),
                      "SHA256:a", path=path)


def test_a_wrong_secret_and_an_unknown_id_look_the_same(tmp_path):
    """Telling them apart would confirm to a guesser which ids exist."""
    path = tmp_path / "invites.yaml"
    inv_, _ = invites.create(path=path)
    wrong = invites.mac(invites.secret_key("guess"), "m")
    with pytest.raises(invites.InviteError) as a:
        invites.check(inv_.id, "m", wrong, "SHA256:a", path=path)
    with pytest.raises(invites.InviteError) as b:
        invites.check("ffffffff", "m", wrong, "SHA256:a", path=path)
    assert str(a.value) == str(b.value)


# ------------------------------------------------------------- the center's half

def test_a_good_invite_admits_the_machine(fleet_of_two):
    f = fleet_of_two
    invite, secret = _invite(f)
    code, body = _ask(f, _request(f, invite, secret))
    assert code == 200, body
    note = acl.unseal(body, f["cpub"])
    assert note["fleet_id"] == f["fleet_id"]
    with being(f["c"]):
        acc = acl.load()
        fp = acl.fingerprint(f["jpub"])
        assert acc.keys[fp]["name"] == "newbox"
        assert acc.keys[fp]["user"] == "alice"
        dev = inv.find_exact(inv.load(), "newbox")
        assert dev.endpoints[0]["target"] == "10.0.0.7", "the address it called from"
        assert dev.endpoints[0]["user"] == "alice"
        assert dev.ssh_auth == "keys"
        assert invites.load()[0].state() == "used"
        assert invites.load()[0].used_as == "newbox"


def test_the_route_the_machine_gives_wins_over_the_address_it_called_from(fleet_of_two):
    """It knows how it is reached; behind NAT the address the center sees is not it."""
    f = fleet_of_two
    invite, secret = _invite(f)
    dev = _me(target="newbox.example.ts.net")
    code, body = _ask(f, _request(f, invite, secret, dev=dev))
    assert code == 200, body
    with being(f["c"]):
        ep = inv.find_exact(inv.load(), "newbox").endpoints[0]
    assert ep["target"] == "newbox.example.ts.net"
    assert ep["via"] == "mesh"


def test_the_invite_names_the_machine(fleet_of_two):
    f = fleet_of_two
    invite, secret = _invite(f, name="gpu-1")
    assert _ask(f, _request(f, invite, secret))[0] == 200
    with being(f["c"]):
        assert inv.find_exact(inv.load(), "gpu-1") is not None
        assert acl.load().name_of(acl.fingerprint(f["jpub"])) == "gpu-1"


def test_a_second_machine_cannot_use_the_same_invite(fleet_of_two, tmp_path):
    f = fleet_of_two
    invite, secret = _invite(f)
    assert _ask(f, _request(f, invite, secret))[0] == 200
    other, other_pub = _keypair(tmp_path, "other")
    code, body = _ask(f, _request(f, invite, secret, dev=_me("other", "id:other"),
                                  key=other))
    assert code == 403 and "already been used" in body
    with being(f["c"]):
        assert acl.fingerprint(other_pub) not in acl.load().keys
        assert inv.find_exact(inv.load(), "other") is None


def test_the_same_machine_may_ask_again(fleet_of_two):
    """Its first reply was lost. Refusing it would strand a machine the owner admitted."""
    f = fleet_of_two
    invite, secret = _invite(f)
    assert _ask(f, _request(f, invite, secret))[0] == 200
    code, body = _ask(f, _request(f, invite, secret))
    assert code == 200, body
    with being(f["c"]):
        assert [m["name"] for m in acl.load().keys.values()].count("newbox") == 1
        assert [d.name for d in inv.live(inv.load())].count("newbox") == 1


def test_a_mac_lifted_off_one_request_is_worthless_on_another(fleet_of_two, tmp_path):
    """Watching the listener's plain HTTP must not be a way to join."""
    f = fleet_of_two
    invite, secret = _invite(f)
    seen = __import__("yaml").safe_load(_request(f, invite, secret))
    thief, thief_pub = _keypair(tmp_path, "thief")
    theirs = acl.seal(inv.dumps([_me("thief", "id:thief")]), key_path=thief)
    forged = join_mod._request_body(invite.id, theirs, seen["mac"])
    code, _ = _ask(f, forged)
    assert code == 403
    with being(f["c"]):
        assert acl.fingerprint(thief_pub) not in acl.load().keys
        assert invites.load()[0].state() == "open", "not spent on a refusal"


def test_a_request_not_signed_by_the_key_it_carries_is_refused(fleet_of_two, tmp_path):
    f = fleet_of_two
    invite, secret = _invite(f)
    yaml = __import__("yaml")
    _, someone_else = _keypair(tmp_path, "someone")
    env = yaml.safe_load(acl.seal(inv.dumps([_me()]), key_path=f["jkey"]))
    env["center_pubkey"] = someone_else              # claims a key it cannot sign with
    sealed = yaml.safe_dump(env, sort_keys=False)
    request = join_mod._request_body(
        invite.id, sealed, invites.mac(invites.secret_key(secret), sealed))
    code, _ = _ask(f, request)
    assert code == 400
    with being(f["c"]):
        assert invites.load()[0].state() == "open"
        assert acl.fingerprint(someone_else) not in acl.load().keys


def test_a_machine_cannot_join_as_one_already_pinned(fleet_of_two):
    """Claiming box's machine-id is either a rebuilt box or an impersonation, and an
    invite is not the place to decide which."""
    f = fleet_of_two
    invite, secret = _invite(f)
    code, body = _ask(f, _request(f, invite, secret, dev=_me("box2", "id:box")))
    assert code == 409, body
    with being(f["c"]):
        assert invites.load()[0].state() == "open"


def test_an_invited_name_already_taken_is_refused(fleet_of_two):
    f = fleet_of_two
    invite, secret = _invite(f, name="box")
    code, body = _ask(f, _request(f, invite, secret))
    assert code == 409 and "already the name" in body


def test_a_name_the_machine_suggests_is_made_unique(fleet_of_two):
    f = fleet_of_two
    invite, secret = _invite(f)
    assert _ask(f, _request(f, invite, secret, dev=_me("box", "id:new")))[0] == 200
    with being(f["c"]):
        assert acl.load().name_of(acl.fingerprint(f["jpub"])) == "box-2"


def test_a_machine_with_no_machine_id_gets_one_from_its_key(fleet_of_two):
    """Measured from the inside, the fallback id is `net:localhost:22` -- the same for
    every such machine, and two of them would merge into one record."""
    f = fleet_of_two
    invite, secret = _invite(f)
    assert _ask(f, _request(f, invite, secret, dev=_me(dev_id="net:localhost:22")))[0] == 200
    with being(f["c"]):
        dev = inv.find_exact(inv.load(), "newbox")
    assert dev.id == f"key:{acl.fingerprint(f['jpub'])}"


def test_with_no_route_and_no_usable_address_it_says_how_to_give_one(fleet_of_two):
    f = fleet_of_two
    invite, secret = _invite(f)
    code, body = _ask(f, _request(f, invite, secret), peer="127.0.0.1")
    assert code == 400 and "--ssh" in body
    with being(f["c"]):
        assert invites.load()[0].state() == "open"


def test_a_machine_that_is_not_the_center_admits_nobody(fleet_of_two):
    f = fleet_of_two
    with being(f["c"]):
        invite, secret = invites.create()
    # j has no access list at all
    code, _ = join_mod.handle(_request(f, invite, secret), peer="10.0.0.7")
    assert code == 503


def test_the_joiner_cannot_make_itself_the_center(fleet_of_two):
    f = fleet_of_two
    invite, secret = _invite(f)
    dev = _me()
    dev.role = "center"
    assert _ask(f, _request(f, invite, secret, dev=dev))[0] == 200
    with being(f["c"]):
        assert inv.find_exact(inv.load(), "newbox").role == "none"
        assert acl.is_center()


# ------------------------------------------------------------------ the joiner's half

@pytest.fixture
def joining(fleet_of_two, monkeypatch, tmp_path):
    """Wire the joiner's `post` to the center's `handle`, each in its own state."""
    f = fleet_of_two
    import fleet.onboard
    from fleet.ops import sync

    monkeypatch.setattr(fleet.onboard, "onboard_self",
                        lambda **k: (_me(name=k.get("name") or "newbox"), None))
    monkeypatch.setattr(join_mod, "local_user", lambda: "alice")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    sent = []

    def post(url, payload, **k):
        sent.append(url)
        code, body = _ask(f, payload)
        return body if code == 200 else (code, body)

    monkeypatch.setattr(sync, "post", post)
    f["home"], f["sent"] = home, sent
    return f


def _code(f, **kw):
    invite, secret = _invite(f, **kw)
    return join_mod.encode_code("http://hub.example:7373/sync", f["cfp"], invite.id, secret)


def test_joining_leaves_a_machine_the_center_can_manage(joining):
    f = joining
    out = join_mod.join(_code(f))
    # the join, then the signed confirmation that the center's key is in place here
    assert f["sent"] == ["http://hub.example:7373/join", "http://hub.example:7373/sync"]
    assert out["name"] == "newbox" and out["center_key_installed"], out
    # the center is pinned, and it is the one the code named
    assert acl.fingerprint(acl.trusted_center_pubkey()) == f["cfp"]
    assert acl.center_url() == "http://hub.example:7373/sync"
    # the fleet arrived
    assert {d.name for d in inv.live(inv.load())} >= {"hub", "box", "newbox"}
    # the center's key is in our own authorized_keys, as the block the reconciler writes
    keys = (f["home"] / ".ssh" / "authorized_keys").read_text(encoding="utf-8")
    assert f"# fleet:{f['fleet_id']}:begin from={f['cfp']} user=alice" in keys
    assert f["cpub"] in keys


def test_joining_twice_keeps_one_block(joining):
    f = joining
    code = _code(f)
    join_mod.join(code)
    join_mod.join(code)
    keys = (f["home"] / ".ssh" / "authorized_keys").read_text(encoding="utf-8")
    assert keys.count(f["cpub"]) == 1


def test_an_answer_from_any_other_key_pins_nothing(joining, monkeypatch, tmp_path):
    """The point of the fingerprint in the code: no trust on first use."""
    f = joining
    from fleet.ops import sync

    impostor, _ = _keypair(tmp_path, "impostor")
    forged = acl.seal(inv.dumps([Device(id="id:evil", name="evil")]), key_path=impostor,
                      fleet_id=f["fleet_id"], center_url="http://evil.example/sync")
    monkeypatch.setattr(sync, "post", lambda *a, **k: forged)
    with pytest.raises(FleetError, match="not from the center the invite named"):
        join_mod.join(_code(f))
    assert acl.trusted_center_pubkey() == ""
    assert "evil" not in {d.name for d in inv.live(inv.load())}
    assert not (f["home"] / ".ssh" / "authorized_keys").exists()


def test_a_refusal_says_why(joining):
    f = joining
    code = _code(f)
    with being(f["c"]):
        invites.revoke(invites.load()[0].id)
    with pytest.raises(FleetError, match="withdrawn"):
        join_mod.join(code)


def test_nothing_answering_says_so(joining, monkeypatch):
    from fleet.ops import sync

    monkeypatch.setattr(sync, "post", lambda *a, **k: (None, "connection refused"))
    with pytest.raises(FleetError, match="no answer"):
        join_mod.join(_code(joining))


def test_a_machine_in_another_fleet_must_leave_first(joining, tmp_path):
    _, other = _keypair(tmp_path, "other-center")
    acl.pin_center_pubkey(other)
    with pytest.raises(FleetError, match="another fleet"):
        join_mod.join(_code(joining))


def test_a_center_with_machines_cannot_join(joining):
    f = joining
    with being(f["c"]):
        with pytest.raises(FleetError, match="other machines in it"):
            join_mod.join(_code(f))


def test_the_empty_fleet_an_install_started_steps_aside_for_a_join(joining, monkeypatch, tmp_path):
    """Every install makes its machine a center, so a machine meant to join someone
    else's fleet is, by then, the center of an empty one."""
    from fleet import service

    f = joining
    monkeypatch.setattr(service, "remove", lambda: "removed")
    _, mine = _keypair(f["j"], "own")
    acl.bootstrap("newbox", f["jpub"], "linux:machine-id:new")   # j's own empty fleet
    assert acl.is_center()
    out = join_mod.join(_code(f))
    assert out["fleet_id"] == f["fleet_id"]
    assert not acl.ACCESS_PATH.exists() and not acl.is_center()
    assert acl.fingerprint(acl.trusted_center_pubkey()) == f["cfp"]


# ------------------------------------------------------------------ the listener

def test_the_listener_routes_joins(fleet_of_two):
    import urllib.error
    import urllib.request

    from fleet import serve

    f = fleet_of_two
    with being(f["c"]):
        httpd, url = serve.serve_in_thread()
        try:
            req = urllib.request.Request(url.replace("/sync", "/join"), data=b"nonsense",
                                         method="POST")
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)
            assert caught.value.code == 400, "routed to the join handler, not a 404"
        finally:
            httpd.shutdown()
            httpd.server_close()


# ------------------------------------------------------------------ the commands

def test_invite_prints_a_code_for_this_center(fleet_of_two, monkeypatch):
    from typer.testing import CliRunner

    from fleet import cli

    f = fleet_of_two
    monkeypatch.setattr(cli, "_listening", lambda url: True)
    with being(f["c"]):
        out = CliRunner().invoke(cli.app, ["invite", "gpu-1", "--json"])
    assert out.exit_code == 0, out.output
    data = __import__("json").loads(out.output)
    parts = join_mod.decode_code(data["code"])
    assert parts["center"] == f["cfp"] and data["name"] == "gpu-1"
    assert data["command"] == f"fleet join {data['code']}"
    # And the line for a machine that has no fleet yet: installs it as a member.
    assert data["install"].endswith(f"install.sh | sh -s -- --join {data['code']}")
    assert f"$env:FLEET_JOIN='{data['code']}'" in data["install_windows"]


def test_invite_shows_the_install_line_whole(fleet_of_two, monkeypatch):
    """Long lines, printed unwrapped: a code split across two lines does not paste."""
    from typer.testing import CliRunner

    from fleet import cli

    monkeypatch.setattr(cli, "_listening", lambda url: True)
    with being(fleet_of_two["c"]):
        out = CliRunner().invoke(cli.app, ["invite"])
    assert out.exit_code == 0, out.output
    lines = [ln.strip() for ln in out.output.splitlines()]
    code = next(ln for ln in lines if ln.startswith("fleet join "))[len("fleet join "):]
    assert any(ln.startswith("curl -LsSf ") and ln.endswith(f"--join {code}") for ln in lines)
    assert any(ln.startswith("$env:FLEET_JOIN=") and ln.endswith("| iex") for ln in lines)


def test_center_json_names_the_install_mode(fleet_of_two):
    """One field for what the installers and agents ask: center or member."""
    import json

    from typer.testing import CliRunner

    from fleet import cli

    with being(fleet_of_two["c"]):
        out = CliRunner().invoke(cli.app, ["center", "--json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["role"] == "center" and data["is_center"] is True


def test_invite_refuses_off_the_center(fleet_of_two):
    from typer.testing import CliRunner

    from fleet import cli

    out = CliRunner().invoke(cli.app, ["invite"])
    assert out.exit_code == 2


@pytest.mark.parametrize("text,seconds", [("15m", 900), ("2h", 7200), ("1d", 86400),
                                          ("90s", 90), ("10", 600), ("soon", 0)])
def test_durations(text, seconds):
    from fleet.cli import _duration_s

    assert _duration_s(text) == seconds



# ------------------------------------------------- the invite path, finished

def test_after_joining_the_centers_access_reads_present(joining, monkeypatch):
    """It read "not applied yet" until a sweep, though the joiner had placed the key --
    and for a machine the center cannot dial, no sweep ever comes."""
    from fleet import reconcile as rec
    from fleet import serve
    from fleet.ops import sync

    f = joining
    def post(url, payload, **k):
        f["sent"].append(url)
        with being(f["c"]):
            if url.endswith("/join"):
                code, body = join_mod.handle(payload, peer="10.0.0.7",
                                             center_url="http://hub.example:7373/sync")
            else:
                code, body = serve.exchange(payload)
        return body if code == 200 else (code, body)
    monkeypatch.setattr(sync, "post", post)
    join_mod.join(_code(f))
    with being(f["c"]):
        st = rec.load_ledger()[f"{f['cfp']}>{acl.fingerprint(f['jpub'])}>alice"]
    assert st.observed == "present"


def test_a_clone_joining_on_an_invite_is_kept_apart(fleet_of_two, tmp_path):
    """Same machine-id as box, a different key, a different hostname: a clone of box's
    image, not box rebuilt. Refusing it as an impersonation left it no way in."""
    from fleet.models import ProbeResult, Snapshot, Status

    f = fleet_of_two
    with being(f["c"]):
        conn = store.connect()
        store.record(conn, "id:box", ProbeResult(status=Status.OK,
                                                 snapshot=Snapshot(ts=1, hostname="box")))
        conn.close()
    invite, secret = _invite(f)
    request = join_mod.build_request(_me("box-clone", "id:box"), invite_id=invite.id,
                                     secret=secret, key_path=f["jkey"], hostname="box-clone")
    code, body = _ask(f, request)
    assert code == 200, body
    with being(f["c"]):
        names = {d.name: d.id for d in inv.live(inv.load())}
    assert names["box-clone"] == "id:box:box-clone" and names["box"] == "id:box"


def test_the_same_machine_rebuilt_is_still_refused(fleet_of_two):
    """Same id and same hostname with a new key is a rebuild or an impersonation, and an
    invite is not where to decide which."""
    from fleet.models import ProbeResult, Snapshot, Status

    f = fleet_of_two
    with being(f["c"]):
        conn = store.connect()
        store.record(conn, "id:box", ProbeResult(status=Status.OK,
                                                 snapshot=Snapshot(ts=1, hostname="box")))
        conn.close()
    invite, secret = _invite(f)
    request = join_mod.build_request(_me("box", "id:box"), invite_id=invite.id,
                                     secret=secret, key_path=f["jkey"], hostname="box")
    assert _ask(f, request)[0] == 409


def test_a_name_that_will_not_resolve_does_not_stall_the_invite(monkeypatch):
    """urlopen's timeout covers the connection, not the name lookup: on a real macOS
    runner the center's `.local` name took 35s to resolve and every invite sat silent."""
    import threading
    import time
    import urllib.request

    from fleet import cli

    release = threading.Event()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: release.wait(30))
    started = time.monotonic()
    try:
        assert cli._listening("http://slow.local:7373/sync", limit_s=0.3) is None
        assert time.monotonic() - started < 2
    finally:
        release.set()
