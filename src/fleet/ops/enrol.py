"""Getting a machine into the fleet, and giving it an identity of its own.

Two halves that are easy to confuse. Installing *our* key on a machine makes it reachable;
pinning *its* key is what makes it grantable, because the access list is keyed on the
fingerprint of the machine's own key. Enrolment used to stop at the first, which produced
hosts the centre could reach and could never grant anything to.
"""

from __future__ import annotations

import getpass
import sys

from .. import inventory as inv
from .. import store
from ..keys import (ensure_keypair, install_key, install_key_over_existing_access,
                    pty_available)
from ..models import Status
from ..probe.runner import run_probe
from ..sshcmd import remote_platform
from ..ui import console, err

def confirm_key(dev, ep) -> None:
    """Re-probe after an install, rather than recording a verdict.

    Auth state is derived from the last probe, so without this the device keeps
    reporting needs_key. It also proves the key actually works -- an append that exits 0
    is not the same as a key sshd will accept, and on Windows the two differ routinely.
    """
    conn = store.connect()
    try:
        store.record(conn, dev.id, run_probe(ep, mode=dev.probe_mode,
                                             disk_paths=dev.disk_paths))
    finally:
        conn.close()

def install_our_key(dev, *, quiet: bool = False) -> bool:
    """Get this machine's fleet key into a host's authorized_keys, cheapest way first.

    The three ways in, in the order that asks least of the user:

    1. **Access we already hold** -- a key in your agent, the one the provider injected
       at creation, or this fleet's own key pre-placed by hand. Costs one connection to
       find out, never prompts, and is the normal case on a cloud VM. Trying it first is
       what lets an agent enrol a machine unattended.
    2. **A password**, typed once and spent on a single connection. Needs a human, so it
       needs a terminal.
    3. **Neither** -- say so, and name the way out: put the key on the host out of band.

    Only ever called for a host that rejected us, so step 1 cannot append a key the host
    already has.
    """
    eps = inv.endpoints_of(dev)
    if not eps:
        err.print(f"[red]{dev.name} has no endpoint recorded[/red]")
        return False
    # The fleet key, never one from ~/.ssh. This key is fleet's handle on the machine:
    # it can be revoked fleet-wide without touching the key you push to GitHub with, and
    # the entry it leaves in authorized_keys says where it came from.
    try:
        path, pubkey = ensure_keypair()
    except KeyError as exc:
        err.print(f"[red]{exc}[/red]")
        return False
    ep = sorted(eps, key=lambda e: e.preference)[0]

    ok, output = install_key_over_existing_access(ep, pubkey)
    if ok:
        confirm_key(dev, ep)
        console.print(f"[green]✓[/green] key installed on {dev.name}, "
                      "over access it already accepted.")
        return True

    if not pty_available():
        # A center running on Windows. Everything else in fleet is portable; driving a
        # password prompt is not, because there is no pty there. Say so as a property of
        # this machine rather than of the host we are enrolling, and name the way round
        # it -- which needs no password anywhere.
        msg = (f"  [dim]{dev.name} accepts no key of ours, and this machine cannot type "
               "a password (no pty on Windows). Put [bold]fleet center --pubkey[/bold] "
               "on it and add it again.[/dim]")
        (console if quiet else err).print(msg)
        return False
    if not sys.stdin.isatty():
        # Hanging on a prompt would be bad; capturing the password into whatever called
        # us would be worse. Refuse, and say exactly what to do instead.
        msg = (f"  [dim]{dev.name} accepts no key of ours and there is no terminal to "
               "type a password. Put [bold]fleet center --pubkey[/bold] on it, or run "
               "[bold]fleet add[/bold] yourself from a terminal.[/dim]")
        (console if quiet else err).print(msg)
        return False

    console.print(f"[dim]installing {path} on {ep.user}@{ep.target}[/dim]")
    password = getpass.getpass(f"Password for {ep.user}@{ep.target}: ")
    try:
        ok, output = install_key(ep, password, pubkey)
    finally:
        password = ""                      # not security, just hygiene: drop it promptly
    if ok:
        confirm_key(dev, ep)
        console.print(f"[green]✓[/green] key installed on {dev.name}; password discarded.")
        return True
    err.print(f"[red]Could not install the key.[/red]\n{output.strip()[-400:]}")
    err.print(f"  [dim]put [bold]fleet center --pubkey[/bold] on {dev.name} "
              "and add it again[/dim]")
    return False

def register_identity(dev) -> str:
    """Give the machine its own fleet keypair and pin it. Returns the fingerprint.

    Enrolment used to stop at "our key is on it", which makes a host reachable and
    nothing else: the access list is keyed on the fingerprint of *its* key, so without
    this the very next step it tells you to run -- granting it something -- could not
    find it. `access.enroll` existed and was never called.

    The key is read back over our own connection rather than taken from anything the
    machine published, so what gets pinned is what we saw on the host itself.
    """
    from .. import access as acl
    from .. import reconcile as rec
    from ..keys import ensure_remote_keypair_command

    try:
        acc = acl.load()
    except acl.AccessError:
        console.print("  [dim]no fleet here yet -- run [bold]fleet center --init[/bold] "
                      "and enrol again to register its key[/dim]")
        return ""

    eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
    conn = store.connect()
    try:
        _, snap = store.latest(conn, dev.id)
    finally:
        conn.close()
    plat = remote_platform(snap)
    ok, out = rec._remote(eps[0], ensure_remote_keypair_command(platform=plat),
                          platform=plat, capture=True)
    pub = next((ln.strip() for ln in (out or "").splitlines()
                if ln.strip().startswith("ssh-")), "")
    if not ok or not pub:
        err.print(f"  [yellow]could not read a key from {dev.name}[/yellow] "
                  f"[dim]{(out or '')[:80]}[/dim]")
        # Do not claim reachability we have not established: this branch is reached just
        # as often because the host refused the connection as because it answered and
        # had no key, and "it is reachable, but..." about a dead rental is a wrong
        # answer printed confidently.
        err.print("  [dim]until it has one it cannot be granted access to anything"
                  "[/dim]")
        return ""
    try:
        fp = acl.enroll(acc, dev.name, pub, dev.id, user=eps[0].user or "root")
    except acl.AccessError as exc:
        err.print(f"  [red]{exc}[/red]")
        return ""
    acl.save(acc)
    console.print(f"[green]✓[/green] {dev.name} registered as {fp[:24]}...")
    return fp

def finish_add(dev, res, *, where: str, this_machine: bool) -> str:
    """Finish the half of adding that only the center can do. Returns what happened.

    `enrolled` means ready to use. Anything else means the machine is recorded and
    cannot yet be granted access to anything, which is a different thing to tell someone
    than "added" -- and is why the outcome is reported rather than implied.
    """
    if this_machine:
        return "self"                      # we are already ourselves; nothing to enrol
    if where != "center":
        # Only the center can write the access list, so that half waits for it.
        console.print(f"  [dim]recorded. The center enrols {dev.name} on its next "
                      "sweep — it is not grantable until then.[/dim]")
        return "pending-center"
    if dev.ssh_auth == "external":
        # Tailscale SSH, Netbird SSH and the like terminate ssh themselves and authorize
        # from their own ACL, so authorized_keys is not consulted. Writing one would
        # report success and grant nothing.
        console.print(f"  [dim]{dev.name} authorizes ssh upstream, not from "
                      "authorized_keys — there is nothing here for the center to "
                      "install[/dim]")
        return "external"
    # A host that already accepts our key needs no install, only an identity.
    if res.status is Status.AUTH_FAILED and not install_our_key(dev):
        console.print(f"  [dim]{dev.name} is recorded, but cannot be granted anything "
                      "until it accepts a key from here[/dim]")
        return "failed"
    return "enrolled" if register_identity(dev) else "failed"
