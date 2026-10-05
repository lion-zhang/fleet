"""Giving the centre away.

Two phases, and the split is the point. The outgoing centre can install the successor's
key everywhere and sign a record naming it, but it cannot prove the successor can
*write* each authorized_keys -- that takes the successor trying, and on Windows both ways
it fails are silent. So the successor finishes the job from its own side, and only then
is the old centre retired.

Tested end to end on a real fleet for the first time, it could not complete at all: the
signed record was written to the *outgoing* centre's own disk, the successor never had
the access list `--accept` loads, and no member knew what a handover record was -- so an
accepted successor's envelopes would have been refused everywhere. What it does now:

1. `fleet center NAME`, on the centre. Grants the successor every machine, as the user
   each is reached as, and applies those grants now. Then delivers, over ssh, the access
   list, the ledger and the signed handover record -- the whole bundle signed again by
   the outgoing centre, which the successor has pinned.
2. `fleet center --accept`, on the successor. Proves it can write every machine, takes
   the role, and sweeps. Every envelope it signs from then on carries the chain of
   handover records, so a member still trusting the old key walks to the new one on the
   strength of the old centre's signature -- never on first use.
3. The outgoing centre steps down by itself the next time it acts as centre: it asks the
   successor, and a reply signed by the successor and carrying the record that names it
   is proof the role moved. It keeps working as a member; its own key comes off every
   machine on the new centre's first sweep, because the new list no longer wants it.

There is no self-promotion anywhere in here, deliberately: a path by which a machine can
declare itself the centre is a hostile-takeover primitive whose guards are themselves
security-critical code.
"""

from __future__ import annotations

import os
import time

import yaml

from ..config import DEFAULT_PORT
from ..state import access as acl
from ..state import inventory as inv
from .. import reconcile as rec
from ..ssh.authkeys import sync_command
from ..ssh.cmd import build_argv, remote_platform, run as sshrun
from ..ssh.keys import ensure_keypair
from ..state import store
from ..ui import console, err
from . import enrol  # noqa: F401  (tests patch ensure_keypair through here)
from .errors import FleetError
from .names import canonical

BUNDLE_KIND = "fleet-handover-bundle"


# ------------------------------------------------------------------- phase one

def give_away(acc, name: str, *, force: bool) -> None:
    """Give the role away. The one irreversible command in the tool."""
    from .sweep import apply_now, endpoint_for

    if not acl.is_center(acc):
        err.print("[red]Only the center can hand the role over.[/red]")
        err.print(f"  [dim]the center is {acc.name_of(acc.center)}[/dim]")
        raise FleetError("only the center can hand the role over", code=2)
    try:
        successor = acl.resolve(acc, canonical(name))
    except acl.AccessError:
        dev = inv.find_exact(inv.load(), name)
        hits = [fp for fp, m in acc.keys.items() if dev and m.get("device_id") == dev.id]
        if len(hits) != 1:
            err.print(f"[red]no machine called {name!r} in the access list[/red]")
            raise FleetError(f"no machine called {name!r}", code=2)
        successor = hits[0]
    if successor == acc.center:
        console.print(f"[dim]{name} is already the center.[/dim]")
        return
    fp = successor
    meta = acc.keys.get(fp) or {}
    if not meta.get("pubkey"):
        err.print(f"[red]No pinned key for {name}.[/red]  Enrol it first:  "
                  f"[bold]fleet sync[/bold]")
        raise FleetError(f"no pinned key for {name}", code=2)
    dev = next((d for d in inv.live(inv.load()) if d.id == meta.get("device_id")), None)
    ep = endpoint_for(dev, meta.get("user", "root")) if dev else None
    if ep is None:
        err.print(f"[red]{acc.name_of(fp)} has no address recorded[/red], so the handover "
                  "cannot be delivered to it")
        raise FleetError("successor has no endpoint", code=2)

    # Every machine but the two ends, as the user that machine is reached as. Granting
    # as root everywhere left the successor pending forever on a machine only reached as
    # alice; granting it the outgoing center left it pending on a machine with no route.
    added = []
    for other, other_meta in acc.keys.items():
        if other in (fp, acc.center):
            continue
        user = other_meta.get("user", "root")
        if acl.grant(acc, fp, other, user=user, note="handover"):
            added.append((other, user))
    acl.save(acc)
    for other, user in added:
        apply_now(acc, fp, other, user, install=True)
    console.print(f"[green]✓[/green] {acc.name_of(fp)} granted {len(added)} machine(s)")

    record = acl.handover_record(acc, fp)
    body = yaml.safe_dump({
        "record": record, "signature": acl.sign(record),
        "chain": acl.handover_chain(),
        "access": acl.dumps(acc),
        # The inventory too: a successor sweeping from its own older copy tried to enrol
        # a machine removed since, and reported renamed machines by their old names.
        "inventory": inv.dumps(inv.load()),
        "ledger": acl.LEDGER_PATH.read_text() if acl.LEDGER_PATH.exists() else "",
    }, sort_keys=False)
    bundle = yaml.safe_dump({"kind": BUNDLE_KIND, "body": body,
                             "signature": acl.sign(body)}, sort_keys=False)

    remote = 'sh -lc \'PATH="$HOME/.local/bin:$PATH" fleet center --receive\''
    proc = sshrun(build_argv(ep, remote=remote), input=bundle.encode(), timeout=60)
    if proc.returncode != 0:
        out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
        err.print(f"[red]Could not deliver the handover to {acc.name_of(fp)}:[/red] "
                  f"{out[-200:]}")
        err.print("  [dim]it needs fleet installed there ([bold]fleet install "
                  f"{acc.name_of(fp)}[/bold]); run this again once it does. Nothing has "
                  "been retired.[/dim]")
        raise FleetError("handover not delivered", code=1)

    acl.HANDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    acl.HANDING_PATH.write_text(yaml.safe_dump({
        "to": fp, "to_name": acc.name_of(fp), "to_pubkey": meta["pubkey"],
        "url": f"http://{ep.target}:{DEFAULT_PORT}/sync", "at": int(time.time()),
    }, sort_keys=False))
    console.print(f"[green]✓[/green] handover delivered to {acc.name_of(fp)}")
    console.print(f"\n  next, on {acc.name_of(fp)}: [bold]fleet center --accept[/bold]")
    console.print(f"\n[dim]It checks it can write every machine before taking the role. "
                  f"Until it does, this machine stays the center -- but make access "
                  f"changes after, not before: {acc.name_of(fp)} now holds a copy of the "
                  f"list as it is.[/dim]")


# --------------------------------------------------------------- on the successor

def receive(raw: str) -> str:
    """Store a handover the center delivered. Verified, never taken on trust.

    Signed by the key this machine pinned, and naming this machine's own key -- anything
    else is refused before a byte is written. Stored, not acted on: taking the role is
    `--accept`, which proves this machine can do the job first.
    """
    pinned = acl.trusted_center_pubkey()
    if not pinned:
        raise FleetError("this machine has no center pinned, so no handover can be "
                         "verified here", code=2)
    try:
        outer = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise FleetError(f"unreadable handover: {exc}", code=2) from exc
    if not isinstance(outer, dict) or outer.get("kind") != BUNDLE_KIND:
        raise FleetError("not a handover", code=2)
    body = str(outer.get("body") or "")
    if not acl.verify(body, str(outer.get("signature") or ""), pinned):
        raise FleetError("this handover is not signed by the center this machine "
                         "trusts", code=2)
    inner = yaml.safe_load(body) or {}
    record = yaml.safe_load(inner.get("record") or "") or {}
    _, pub = ensure_keypair()
    if record.get("to") != acl.fingerprint(pub):
        raise FleetError("this handover names a different machine", code=2)
    acl.INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    acl.INBOX_PATH.write_text(yaml.safe_dump({
        "record": inner["record"], "signature": inner.get("signature", ""),
        "chain": inner.get("chain") or []}, sort_keys=False))
    acl.ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = acl.ACCESS_PATH.with_suffix(".tmp")
    tmp.write_text(inner.get("access") or "")
    os.replace(tmp, acl.ACCESS_PATH)
    if inner.get("ledger"):
        acl.LEDGER_PATH.write_text(inner["ledger"])
    if inner.get("inventory"):
        incoming = inv.loads(inner["inventory"])
        inv.update(lambda current: inv.merge(current, incoming, authoritative=True))
    return record.get("fleet_id", "")


def accept(acc) -> None:
    """Phase two, on the successor: prove we can write, then take the role.

    The check is a no-op marker-block edit on every machine -- drop our own block and put
    it straight back. Probing would only prove our key is *present*; it says nothing
    about whether the file can be written, and on Windows every way that fails is
    silent, so a handover verified by probing would hand the fleet to a machine that
    cannot manage it and discover that only after the predecessor was gone.
    """
    from .sweep import endpoint_for, run as sweep

    if acc is None:
        err.print("[red]No handover has reached this machine.[/red] On the center: "
                  "[bold]fleet center THIS-MACHINE[/bold]")
        raise FleetError("no handover received", code=2)
    _, pub = ensure_keypair()
    mine = acl.fingerprint(pub)
    if mine == acc.center:
        console.print("[dim]already the center[/dim]")
        return
    if mine not in acc.keys:
        err.print("[red]This machine is not in the access list,[/red] so no handover "
                  "could have named it.")
        raise FleetError("this machine is not in the access list", code=2)
    inbox = {}
    if acl.INBOX_PATH.exists():
        inbox = yaml.safe_load(acl.INBOX_PATH.read_text()) or {}

    devices = {d.id: d for d in inv.live(inv.load())}
    # Not the outgoing center: it is being retired, it has no route by design, and
    # requiring it to be writable made every real handover refuse.
    targets = [(fp, m) for fp, m in acc.keys.items() if fp not in (mine, acc.center)]
    unwritable = []
    for fp, meta in targets:
        dev = devices.get(meta.get("device_id", ""))
        ep = endpoint_for(dev, meta.get("user", "root")) if dev else None
        if ep is None:
            unwritable.append((meta.get("name", fp[:18]), "no endpoint recorded"))
            continue
        # drop-then-append of our own block: idempotent, and it changes nothing if it
        # works, which is what makes it safe to run as a test
        conn = store.connect()
        try:
            _, snap = store.latest(conn, dev.id)
        finally:
            conn.close()
        plat = remote_platform(snap)
        script = sync_command(acc.fleet_id, mine, user=ep.user, pubkey=pub,
                              platform=plat)
        ok, out = rec._remote(ep, script, platform=plat)
        name = meta.get("name", fp[:18])
        if ok:
            console.print(f"[green]✓[/green] can write {name}")
        else:
            unwritable.append((name, out[:80]))
            console.print(f"[red]✗[/red] {name} [dim]{out[:60]}[/dim]")

    if unwritable:
        err.print(f"\n[red]Not taking the role.[/red] {len(unwritable)} machine(s) "
                  "cannot be written from here, and a center that cannot write is a "
                  "fleet nobody can manage:")
        for name, why in unwritable:
            err.print(f"  {name}: {why}")
        err.print("\n  [dim]the current center still holds the role; fix these and "
                  "run this again[/dim]")
        raise FleetError("the successor cannot write every machine", code=2)

    outgoing = acc.name_of(acc.center)
    old_fp = acc.center
    acc.center = mine
    # The outgoing center stays in the list -- it is a member now, and its listener
    # requests must still be answered -- but its record has no address by design, so an
    # edge to it would sit "pending" for ever. Marked, until someone gives it a route.
    old_dev = devices.get((acc.keys.get(old_fp) or {}).get("device_id", ""))
    if old_dev is None or not inv.endpoints_of(old_dev):
        acc.keys.setdefault(old_fp, {})["no_route"] = True
    acl.save(acc)
    _retire_key(acc, old_fp, mine)
    if inbox.get("record"):
        acl.save_handover_chain(list(inbox.get("chain") or [])
                                + [{"record": inbox["record"],
                                    "signature": inbox.get("signature", "")}])
        acl.INBOX_PATH.unlink(missing_ok=True)
    # Nothing of the old center's trust is kept: this machine is the center now, and a
    # pinned center key here would make it try to refresh from a machine it replaced.
    acl.CACHE_PATH.unlink(missing_ok=True)
    for d in inv.live(inv.load()):
        if d.id == (acc.keys.get(mine) or {}).get("device_id"):
            def promote(current, my_id=d.id):
                return current, inv.promote_center(current, inv.find_exact(current, my_id))
            inv.update(promote)
            break
    console.print(f"\n[green]✓[/green] this machine is now the center of {acc.fleet_id}.")
    console.print(f"  [dim]sweeping, so every machine learns it -- and {outgoing}'s own "
                  "key comes off them[/dim]")
    sweep(inv.load())
    console.print(f"\n  [dim]start serving here: [bold]fleet service install[/bold] or "
                  f"[bold]fleet center --listen[/bold]. {outgoing} steps down by itself "
                  "the next time it is used as center.[/dim]")


def _retire_key(acc, old_fp: str, mine: str) -> None:
    """Mark the outgoing center's key for removal everywhere, and take it off here now.

    The planner removes what the ledger says is there. A machine that joined on an
    invite placed the center's key itself, so no ledger ever recorded it -- and on a
    real handover the old center's key stayed on exactly those machines, the new center
    among them. Its key is on every machine by construction, so say so outright.
    """
    import subprocess

    from ..ssh.cmd import local_platform, local_shell_argv

    ledger = rec.load_ledger()
    for fp, meta in acc.keys.items():
        if fp in (old_fp, mine):
            continue
        key = ">".join((old_fp, fp, meta.get("user", "root")))
        st = ledger.get(key) or rec.EdgeState()
        st.desired, st.pending_since = "absent", int(time.time())
        st.dst_device = st.dst_device or meta.get("device_id", "")
        ledger[key] = st
    rec.save_ledger(ledger)
    script = sync_command(acc.fleet_id, old_fp, pubkey=None, platform=local_platform())
    subprocess.run(local_shell_argv(), input=script.encode(), capture_output=True)


# --------------------------------------------------------- the outgoing center

def settle(acc) -> bool:
    """On a center that handed the role over: has the successor taken it? Step down if so.

    Proof, not hope: a reply signed by the successor's key, carrying the handover record
    this machine signed. Anything less -- no answer, a reply without the record -- leaves
    this machine the center, which is the safe way to be wrong.
    """
    from .sync import post, telemetry_to_relay

    if not acl.HANDING_PATH.exists() or not acl.is_center(acc):
        return False
    try:
        pending = yaml.safe_load(acl.HANDING_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    url, to_pub = pending.get("url", ""), pending.get("to_pubkey", "")
    if not url or not to_pub:
        return False
    try:
        payload = acl.seal(inv.dumps(inv.load()), telemetry=telemetry_to_relay())
    except Exception:
        return False
    reply = post(url, payload, timeout=5.0)
    if not reply:
        return False
    try:
        env = yaml.safe_load(reply) or {}
        if acl.fingerprint(str(env.get("center_pubkey") or "")) != pending.get("to"):
            return False
        note = acl.unseal(reply, to_pub)
    except Exception:
        return False
    if acl.follow_chain(env.get("handovers") or [], ensure_keypair()[1]).strip() \
            != to_pub.strip():
        return False                       # it answered, but has not taken the role

    retired = acl.ACCESS_PATH.with_name("access.retired.yaml")
    os.replace(acl.ACCESS_PATH, retired)
    acl.HANDING_PATH.unlink(missing_ok=True)
    acl.pin_center_pubkey(to_pub)
    acl.note_center_url(note["center_url"] or url)
    acl.note_center_seen()
    acl.note_fleet_id(note.get("fleet_id", "") or acc.fleet_id)
    console.print(f"[green]✓[/green] {pending.get('to_name', 'the successor')} has taken "
                  f"the role; this machine is a member of {acc.fleet_id} now.")
    console.print(f"  [dim]the list it held is kept at {retired}, for reference "
                  f"only[/dim]")
    return True
