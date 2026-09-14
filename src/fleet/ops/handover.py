"""Giving the centre away.

Two phases, and the split is the point. The outgoing centre can install the successor's
key everywhere and sign a record naming it, but it cannot prove the successor can
*write* each authorized_keys -- that takes the successor trying, and on Windows both ways
it fails are silent. So the successor finishes the job from its own side, and only then
is the old centre retired.

There is no self-promotion anywhere in here, deliberately: a path by which a machine can
declare itself the centre is a hostile-takeover primitive whose guards are themselves
security-critical code.
"""

from __future__ import annotations

import yaml

from .. import access as acl
from .. import inventory as inv
from .. import reconcile as rec
from ..authkeys import sync_command
from ..keys import ensure_keypair
from ..sshcmd import remote_platform
from .. import store
from ..ui import console, err
from . import enrol
from .errors import FleetError
from .names import canonical

def accept(acc) -> None:
    """Phase two, on the successor: prove we can write, then take the role.

    The check is a no-op marker-block edit on every machine -- drop our own block and put
    it straight back. Probing would only prove our key is *present*; it says nothing
    about whether the file can be written, and on Windows every way that fails is
    silent, so a handover verified by probing would hand the fleet to a machine that
    cannot manage it and discover that only after the predecessor was gone.
    """
    _, pub = ensure_keypair()
    mine = acl.fingerprint(pub)
    if mine == acc.center:
        console.print("[dim]already the center[/dim]")
        return
    if mine not in acc.keys:
        err.print("[red]This machine is not in the access list,[/red] so no handover "
                  "could have named it.")
        raise FleetError("this machine is not in the access list", code=2)

    devices = {d.id: d for d in inv.live(inv.load())}
    targets = [(fp, m) for fp, m in acc.keys.items() if fp != mine]
    unwritable = []
    for fp, meta in targets:
        dev = devices.get(meta.get("device_id", ""))
        eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference) if dev else []
        if not eps:
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
        script = sync_command(acc.fleet_id, mine, user=eps[0].user, pubkey=pub,
                              platform=plat)
        ok, out = rec._remote(eps[0], script, platform=plat)
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

    acc.center = mine
    acl.save(acc)
    console.print(f"\n[green]✓[/green] this machine is now the center of {acc.fleet_id}.")
    console.print("  [dim]run [bold]fleet sync[/bold] to sweep, then retire the old one "
                  "with [bold]fleet rm[/bold] on it if it is leaving[/dim]")

def give_away(acc, name: str, *, force: bool) -> None:
    """Give the role away. The one irreversible command in the tool."""

    if not acl.is_center(acc):
        err.print("[red]Only the center can hand the role over.[/red]")
        err.print(f"  [dim]the center is {acc.name_of(acc.center)}[/dim]")
        raise FleetError("only the center can hand the role over", code=2)
    try:
        successor = acl.resolve(acc, canonical(name))
    except acl.AccessError as exc:
        err.print(f"[red]{exc}[/red]")
        raise FleetError(str(exc), code=2)
    if successor == acc.center:
        console.print(f"[dim]{name} is already the center.[/dim]")
        return
    # Phase one. The successor's key goes everywhere and a signed record names it, but
    # nothing is retired yet: proving the successor can *write* each authorized_keys
    # requires the successor to try, and on Windows both ways that fails are silent. So
    # it finishes the job from its own side.
    fp = successor
    pub = (acc.keys.get(fp) or {}).get("pubkey", "")
    if not pub:
        err.print(f"[red]No pinned key for {name}.[/red]  Enrol it first:  "
                  f"[bold]fleet sync[/bold]")
        raise FleetError(f"no pinned key for {name}", code=2)

    added = 0
    for other in acc.keys:
        if other != fp and acl.grant(acc, fp, other):
            added += 1
    acl.save(acc)
    console.print(f"[green]✓[/green] {name} granted access to {added} machine(s)")

    record = acl.handover_record(acc, fp)
    signed = acl.sign(record)
    bundle = acl.CACHE_PATH.with_name("handover.yaml")
    bundle.write_text(yaml.safe_dump({"record": record, "signature": signed},
                                     sort_keys=False))

    console.print(f"\n[bold]Two things left, in this order.[/bold]")
    console.print(f"  1. [bold]fleet sync[/bold] here, to install {name}'s key everywhere")
    console.print(f"  2. on {name}: [bold]fleet center --accept[/bold]")
    console.print(f"\n[dim]It verifies it can actually write each authorized_keys before "
                  f"taking the role -- probing only proves a key is present. Nothing is "
                  f"retired until it succeeds, so this machine stays the center until "
                  f"then.[/dim]")
    console.print(f"[dim]handover record: {bundle}[/dim]")
