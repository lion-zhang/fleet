"""Spending the passwords an older fleet stored.

One-way and one-time: each password buys a key install and is then dropped, and the
encrypted file goes only once every machine in it has been migrated. Kept behind an
optional extra because dropping the reader in the same release that added the migration
would strand the data behind a dependency nobody can install any more.
"""

from __future__ import annotations

from contextlib import suppress

from ..state import inventory as inv
from ..ssh.keys import ensure_keypair, install_key, pty_available
from ..ui import console, err
from .errors import FleetError

def run() -> None:
    """Spend each stored password once, to install this machine's fleet key.

    Install, verify, then remove -- in that order, never the reverse. A password dropped
    before the key is proven leaves a host nobody can reach, and the whole point of the
    change is that there is no second copy of it anywhere.

    Reading them needs `pyrage`, which is now an optional extra. That is deliberate: the
    encrypted file is still on disk, and removing the only thing that can read it in the
    same release that added the migration would strand it.
    """
    from .. import secrets as sec

    try:
        data = sec.read_secrets(sec.SECRETS_PATH, sec.load_identity())
    except Exception as exc:
        err.print(f"[red]Cannot read the old secrets:[/red] {exc}")
        # escaped: rich reads a bare [migrate] as a style tag and silently eats it,
        # leaving the user an install command that does not install the reader
        err.print("  [dim]install the reader with [bold]uv tool install "
                  r"'agents-fleet\[migrate]'[/bold][/dim]")
        raise FleetError("the migrate extra is not installed", code=2)
    if not data:
        console.print("[dim]nothing stored -- nothing to migrate[/dim]")
        return
    if not pty_available():
        # Typing a stored password needs a pty, and Windows has none: this died there on
        # "No module named 'termios'" with the file intact but nothing said.
        err.print(f"[yellow]Keeping {sec.SECRETS_PATH.name}[/yellow] -- this machine "
                  "cannot type a password (no pty here; Windows has none). Run "
                  "[bold]fleet access --migrate[/bold] on a Linux or macOS machine "
                  "holding a copy, or put [bold]fleet center --pubkey[/bold] on each "
                  "host and delete the file.")
        raise FleetError("this machine cannot type a password", code=1)

    _, pub = ensure_keypair()
    devices = inv.load()
    failed = []
    for name, password in sorted(data.items()):
        dev = inv.find(devices, name)
        eps = inv.endpoints_of(dev) if dev else []
        if not eps:
            failed.append((name, "no endpoint recorded"))
            continue
        ok, out = install_key(sorted(eps, key=lambda e: e.preference)[0], password, pub)
        if ok:
            console.print(f"[green]✓[/green] {name}")
        else:
            failed.append((name, out.strip()[-120:]))
    for name, why in failed:
        err.print(f"[red]✗[/red] {name}: {why}")
    if failed:
        err.print(f"\n[yellow]Keeping {sec.SECRETS_PATH.name}[/yellow] -- "
                  f"{len(failed)} of {len(data)} could not be migrated.")
        raise FleetError(f"{len(failed)} of {len(data)} could not be migrated", code=1)
    # "removed", not "shredded": os.replace on a journalling filesystem or an SSD does
    # not reliably destroy the old blocks, and saying otherwise would be a lie that
    # outlives whoever wrote it.
    for path in (sec.SECRETS_PATH, sec.IDENTITY_PATH):
        with suppress(OSError):
            path.unlink()
    console.print(f"\n[green]✓[/green] all {len(data)} migrated; stored passwords removed.")
