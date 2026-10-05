"""A machine joining the fleet on an invite, both sides of the wire.

The other way in. `fleet add` has the center dial a machine, which needs the center to
reach it and some way in -- a key it already accepts, or a password typed once. Here the
machine dials the center's listener instead, holding an invite the center issued, and
the only human step is copying one code from one terminal to another.

**What the code carries, and why each part is there.**

* The center's address, so the joiner knows where to go.
* The **fingerprint of the center's key**. This is what makes the reply trustworthy
  without trust on first use: the joiner refuses any answer not signed by exactly that
  key, so someone who intercepts the request cannot answer as the center. The ordinary
  sync path has to pin whoever answered first; a join does not.
* The invite's id and secret. The secret never leaves the joiner: what it sends is a MAC
  over its own signed request, so watching the listener's plain HTTP yields nothing that
  could be redeemed for a different key.

**What the center does with a good request.** Pins the joiner's key -- proved by the
joiner's signature over the request, exactly as every sealed envelope proves its signer
-- records the machine, and spends the invite. Nothing is installed anywhere, because
nothing needs to be: the joiner writes the center's key into its *own* authorized_keys,
which is a machine editing a file it owns, and from then on the center manages it the
way it manages every other machine.

Never a password, never a prompt, and safe for a provisioning script to run, since the
code it is handed stops being worth anything once used.
"""

from __future__ import annotations

import base64
import getpass
import ipaddress
import json
import subprocess
import threading

import yaml

from ..models import Device
from ..state import access as acl
from ..state import invites as invites_mod
from ..state import inventory as inv
from ..ssh.cmd import Endpoint, classify_route, local_platform, local_shell_argv, \
    parse_ssh_command
from .errors import FleetError

CODE_PREFIX = "fleet1:"
JOIN_PROTOCOL = 1
MAX_JOIN_BODY = 256 * 1024          # one machine's record and two signatures

# The listener answers on threads, and admitting a machine is load-modify-save of the
# access list. Two joins landing together would each save a list missing the other's
# pin -- invite spent, machine recorded, key not pinned. One admission at a time.
_ADMIT = threading.Lock()


# ------------------------------------------------------------------- the code

def encode_code(url: str, center_fp: str, invite_id: str, secret: str) -> str:
    """One string to paste. Opaque on purpose: nothing in it is meant to be edited."""
    raw = json.dumps({"u": url, "c": center_fp, "i": invite_id, "s": secret},
                     separators=(",", ":"))
    return CODE_PREFIX + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_code(code: str) -> dict:
    """{url, center, id, secret} from a code, or FleetError naming what is wrong with it."""
    code = (code or "").strip()
    if not code.startswith(CODE_PREFIX):
        raise FleetError("that is not a fleet invite code -- it should start with "
                         f"{CODE_PREFIX!r}", code=2)
    body = code[len(CODE_PREFIX):]
    try:
        raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        out = {"url": str(raw["u"]), "center": str(raw["c"]),
               "id": str(raw["i"]), "secret": str(raw["s"])}
    except (ValueError, KeyError, TypeError) as exc:
        raise FleetError(f"that invite code is damaged ({exc.__class__.__name__}) -- "
                         "copy it again, whole", code=2) from exc
    if not all(out.values()) or not out["center"].startswith("SHA256:"):
        raise FleetError("that invite code is incomplete -- copy it again, whole", code=2)
    return out


def join_url(sync_url: str) -> str:
    """Where to send a join, from the address machines are told to sync with."""
    base = sync_url.rstrip("/")
    if base.endswith("/sync"):
        base = base[: -len("/sync")]
    return base + "/join"


# ------------------------------------------------------------------- the request

def _request_body(invite_id: str, sealed: str, mac: str) -> str:
    return yaml.safe_dump({"kind": "fleet-join", "protocol": JOIN_PROTOCOL,
                           "invite": invite_id, "mac": mac, "sealed": sealed},
                          sort_keys=False)


def build_request(device: Device, *, invite_id: str, secret: str,
                  key_path=None, hostname: str = "") -> str:
    """The joiner's half: its own record, sealed with its own key, MAC'd with the invite.

    Sealing is what proves the joiner holds the key it is asking to have pinned -- the
    same proof every envelope in the fleet carries. The MAC is over the whole sealed
    envelope, so the invite cannot be lifted off this request and stapled to another.
    """
    sealed = acl.seal(inv.dumps([device]), key_path=key_path,
                      claims={"hostname": hostname} if hostname else None)
    return _request_body(invite_id, sealed,
                         invites_mod.mac(invites_mod.secret_key(secret), sealed))


def _peer_address(peer: str) -> str:
    """The address a request came from, as something ssh can dial."""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return ""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified:
        return ""
    return str(ip)


def _admitted_record(dev: Device, *, name: str, peer: str) -> Device:
    """What the center will record, from what the joiner said about itself.

    The joiner is trusted to describe itself -- the invite says the owner wanted it in --
    but not to describe the fleet. So it gets one machine, its own, with no role and no
    say over anyone else's record; a route of its choosing, because it is the one that
    knows how it is reached; and nothing that only means something on the joiner, like a
    path to an identity file on its disk.
    """
    eps = []
    for e in (dev.endpoints or [])[:1]:
        target = str(e.get("target") or "") or _peer_address(peer)
        if not target:
            continue
        eps.append({"name": "primary", "target": target, "user": str(e.get("user") or ""),
                    "port": int(e.get("port") or 22), "preference": 10,
                    **({"via": v} if (v := classify_route(target)) else {})})
    if not eps and (target := _peer_address(peer)):
        eps.append({"name": "primary", "target": target, "user": "", "port": 22,
                    "preference": 10,
                    **({"via": v} if (v := classify_route(target)) else {})})
    return Device(id=_stable_id(dev), name=name, kind=dev.kind, alias="",
                  tags=list(dev.tags or []),
                  endpoints=eps, ssh_auth="keys", pubkey=dev.pubkey,
                  probe_policy=dev.probe_policy if dev.probe_policy in
                  ("auto", "on_demand", "never") else "auto")


def _stable_id(dev: Device) -> str:
    """The machine's own id, unless it could not measure one.

    `derive_id` falls back to `net:<target>:<port>` when there is no machine-id, and a
    joiner describes itself from the inside, so that fallback is `net:localhost:22` --
    the same for every machine that hits it. Two of those would merge into one record.
    The key is unique by construction, so it stands in.
    """
    if dev.id and not dev.id.startswith("net:"):
        return dev.id
    try:
        return f"key:{acl.fingerprint(dev.pubkey)}"
    except acl.AccessError:
        return dev.id


def _unique_name(wanted: str, taken: set[str]) -> str:
    name, n = wanted, 2
    while name in taken:
        name, n = f"{wanted}-{n}", n + 1
    return name


def handle(raw: str, *, peer: str = "", center_url: str = "") -> tuple[int, str]:
    """The center's half. Returns (HTTP status, body): a sealed reply, or why not.

    Order matters. Everything that can fail is checked before the invite is spent, and
    the invite is spent before the reply is sent: a machine whose reply is lost can ask
    again with the same key, and nobody else can.
    """
    try:
        acc = acl.load()
    except acl.AccessError as exc:
        return 503, f"{exc}\n"
    if not acl.is_center(acc):
        return 503, "not the center\n"

    try:
        req = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return 400, "unreadable join request\n"
    if not isinstance(req, dict) or req.get("kind") != "fleet-join":
        return 400, "not a join request\n"
    if int(req.get("protocol") or 0) != JOIN_PROTOCOL:
        return 400, f"join protocol {req.get('protocol')!r} is not {JOIN_PROTOCOL}\n"
    sealed = str(req.get("sealed") or "")
    signer = acl.claimed_signer(sealed)
    try:
        fp = acl.fingerprint(signer)
    except acl.AccessError:
        return 400, "the request carries no key\n"

    # The invite before the signature: checking a MAC is a hash, checking a signature is
    # a process. A caller without an invite should cost the center as little as possible.
    try:
        invite = invites_mod.check(str(req.get("invite") or ""), sealed,
                                   str(req.get("mac") or ""), fp)
    except invites_mod.InviteError as exc:
        return 403, f"{exc}\n"

    try:
        note = acl.unseal(sealed, signer)
        offered = inv.loads(note["inventory"])
    except Exception as exc:
        return 400, f"the request is not signed by the key it carries: {exc}\n"
    if len(offered) != 1:
        return 400, "a join request describes exactly one machine\n"
    dev = offered[0]

    with _ADMIT:
        return _admit(dev, signer, fp, invite, peer=peer, center_url=center_url,
                      hostname=str(note.get("claims", {}).get("hostname") or ""))


def _slug(text: str) -> str:
    from ..onboard import slugify

    return slugify(text)


def _is_a_clone(acc, known: Device, fp: str, hostname: str) -> bool:
    if not hostname:
        return False
    pinned = [k for k, m in acc.keys.items() if m.get("device_id") == known.id]
    if not pinned or fp in pinned:
        return False                       # unpinned, or this very key: same machine
    from ..state import store

    conn = store.connect()
    try:
        _, snap = store.latest(conn, known.id)
    finally:
        conn.close()
    before = (snap or {}).get("hostname") or ""
    return bool(before) and before != hostname


def _admit(dev: Device, signer: str, fp: str, invite, *, peer: str,
           center_url: str, hostname: str = "") -> tuple[int, str]:
    """Pin, record and spend, against an access list read under the admission lock."""
    from ..ops.sync import telemetry_to_relay

    try:
        acc = acl.load()
    except acl.AccessError as exc:
        return 503, f"{exc}\n"
    devices = inv.load()
    dev.pubkey = signer                    # the key that signed, not whatever it claimed
    stable = _stable_id(dev)
    known = inv.find_exact(devices, stable) if stable else None
    if known is not None and _is_a_clone(acc, known, fp, hostname):
        # Same machine-id, a different key and a different hostname: another machine
        # cloned from the same image, not this one rebuilt. Kept apart, as `fleet add`
        # does, rather than refused as an impersonation of the first.
        stable, known = f"{stable}:{_slug(hostname)}", None
        dev.name = dev.name if dev.name != "device" else _slug(hostname)
    pinned_name = (acc.keys.get(fp) or {}).get("name", "")
    if pinned_name:
        name = pinned_name                 # this key joined before; keep what it is called
    elif known is not None:
        name = known.name                  # a machine already recorded, e.g. by `fleet add`
    else:
        wanted = invite.name or dev.name or "device"
        taken = inv.handles(devices)
        if invite.name and invite.name in taken:
            return 409, (f"{invite.name} is already the name of another machine -- "
                         "issue a new invite with a different name\n")
        name = _unique_name(wanted, taken)

    record = _admitted_record(dev, name=name, peer=peer)
    record.id = stable
    if not record.endpoints:
        return 400, ("could not tell how the center would reach this machine -- "
                     "join again with --ssh \"ssh user@address\"\n")
    user = record.endpoints[0].get("user") or "root"
    try:
        acl.enroll(acc, name, signer, record.id, user=user)
    except acl.AccessError as exc:
        hint = ""
        if known is not None:
            hint = (f" If this is a different machine from {known.name} -- cloned from the "
                    "same image, so sharing its machine-id -- give it its own "
                    "(`systemd-machine-id-setup` after emptying /etc/machine-id) and "
                    "join again.")
        return 409, f"{exc}{hint}\n"

    try:
        invites_mod.redeem(invite.id, fp, name)
    except invites_mod.InviteError as exc:
        return 403, f"{exc}\n"         # lost a race to another key; nothing was saved
    acl.save(acc)

    def admit(current):
        merged, _ = inv.upsert(current, record)
        if survivor := inv.find_exact(merged, record.id):
            survivor.ssh_auth = "keys"
            if record.pubkey:
                survivor.pubkey = record.pubkey
            inv.touch(survivor)
        return merged, None
    merged, _ = inv.update(admit)

    return 200, acl.seal(inv.dumps(merged), telemetry=telemetry_to_relay(),
                         center_url=center_url, fleet_id=acc.fleet_id)


# ------------------------------------------------------------------- the joiner

def local_user() -> str:
    try:
        return getpass.getuser()
    except Exception:              # no passwd entry, as in some containers
        return ""


def _install_center_key(fleet_id: str, center_fp: str, user: str, pubkey: str) -> tuple[bool, str]:
    """Put the center's key in this machine's own authorized_keys, as a fleet block.

    The same block the center's reconciler writes, labelled the same way, so its first
    sweep finds the edge already in place and has nothing to do -- and `fleet center
    --leave` and `--dissolve` find it to remove like any other.
    """
    from ..ssh.authkeys import sync_command

    script = sync_command(fleet_id, center_fp, user=user, pubkey=pubkey,
                          platform=local_platform())
    p = subprocess.run(local_shell_argv(), input=script.encode(), capture_output=True)
    return p.returncode == 0, (p.stdout + p.stderr).decode(errors="replace")


def _confirm_center_key(url: str) -> None:
    """Tell the center its key is in place here, so `fleet access` says so.

    Best effort: a center that does not hear it learns the same from its first sweep.
    """
    from .sync import post, telemetry_to_relay

    try:
        payload = acl.seal(inv.dumps(inv.load()), telemetry=telemetry_to_relay(),
                           claims={"center_key": "present"})
    except Exception:
        return
    post(url, payload)


def join(code: str, *, name: str = "", ssh_command: str = "") -> dict:
    """Join the fleet the code names. Returns a summary; raises FleetError on refusal."""
    from ..onboard import endpoint_dict, onboard_self
    from ..ssh.keys import ensure_keypair
    from .lifecycle import membership
    from .sync import post, record_relayed

    parts = decode_code(code)

    where = membership()
    if where == "center":
        raise FleetError("this machine is a center already -- a center cannot join "
                         "another fleet", code=2)
    pinned = acl.trusted_center_pubkey()
    if pinned:
        try:
            same = acl.fingerprint(pinned) == parts["center"]
        except acl.AccessError:
            same = False
        if not same:
            raise FleetError("this machine is already in another fleet -- leave it first "
                             "with `fleet center --leave`", code=2)

    try:
        _, pub = ensure_keypair()
    except KeyError as exc:
        raise FleetError(str(exc), code=2) from exc

    dev, res = onboard_self(name=name or None)
    import socket

    hostname = (res.snapshot.hostname if res is not None and res.snapshot else "") \
        or socket.gethostname()
    dev.pubkey = pub
    if ssh_command:
        parsed = parse_ssh_command(ssh_command)
        if not parsed.target:
            raise FleetError(f"no host in {ssh_command!r}", code=2)
        ep = Endpoint(target=parsed.target, user=parsed.user or local_user(),
                      port=parsed.port or 22)
    else:
        # The address is left for the center to fill in from the connection it sees:
        # this machine cannot know which of its addresses the center can route to, and
        # the one the center just heard from is the one that demonstrably works.
        ep = Endpoint(target="", user=local_user(), port=22)
    dev.endpoints = [endpoint_dict(ep)]

    body = build_request(dev, invite_id=parts["id"], secret=parts["secret"],
                         hostname=hostname)
    url = join_url(parts["url"])
    reply = post(url, body, timeout=30.0, errors=True)
    if reply is None:
        reply = (None, "nothing answered")
    if isinstance(reply, tuple):
        status, text = reply
        if status is None:
            raise FleetError(f"no answer from {url}: {text} -- is the center listening? "
                             "(`fleet invite` there warns when it is not)")
        raise FleetError(f"the center refused: {text.strip() or status}")

    # Exactly the key the code named, or nothing. This is the point of carrying the
    # fingerprint: no trust on first use, so nobody between here and the center can
    # answer in its place.
    center_pub = acl.claimed_signer(reply)
    try:
        if acl.fingerprint(center_pub) != parts["center"]:
            raise acl.AccessError("signed by a different key from the one the invite named")
        note = acl.unseal(reply, center_pub)
        incoming = inv.loads(note["inventory"])
    except (acl.AccessError, inv.InventoryError, ValueError) as exc:
        raise FleetError(f"the answer from {url} is not from the center the invite "
                         f"named: {exc}") from exc
    fleet_id = note.get("fleet_id") or ""
    if not fleet_id:
        raise FleetError("the center's answer did not say which fleet this is -- "
                         "update fleet on the center and try again")

    acl.pin_center_pubkey(center_pub)
    _, changes = inv.update(lambda current: inv.merge(current, incoming, authoritative=True))
    if note["telemetry"]:
        record_relayed(note["telemetry"])
    acl.note_center_seen()
    acl.note_center_url(note["center_url"] or parts["url"])
    acl.note_fleet_id(fleet_id)

    # Found by key, not id: the center may have given us an id of its own (see
    # `_stable_id`), and the key is the one thing both sides agree this machine is.
    me = next((d for d in inv.live(incoming) if d.pubkey.strip() == pub.strip()), None)
    joined_as = me.name if me else dev.name
    eps = inv.endpoints_of(me) if me else []
    reach = eps[0] if eps else None
    user = (reach.user if reach else "") or local_user() or "root"
    ok, out = _install_center_key(fleet_id, parts["center"], user, center_pub)
    if ok:
        _confirm_center_key(note["center_url"] or parts["url"])
    reached_as = ""
    if reach:
        reached_as = f"{reach.user}@{reach.target}" if reach.user else reach.target
        if reach.port and reach.port != 22:
            reached_as += f" port {reach.port}"
    return {"fleet_id": fleet_id, "name": joined_as, "machines": len(inv.live(incoming)),
            "changed": changes, "reached_as": reached_as, "center_key_installed": ok,
            "install_output": "" if ok else out.strip()[-300:]}
