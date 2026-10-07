# The center

Every fleet has exactly one **center**. It is the machine you installed fleet on first,
and it:

- holds the **access list** — which machine may reach which;
- holds the key that reaches every machine, and is the only machine that **places or
  removes keys**;
- **answers members** that ask for fresh information, and machines joining with an
  invite.

Every other machine is a **member**. Pick as the center a machine that can reach all your
devices and is online most of the time — a desktop or a home server. A laptop works too.

## When the center is off

Nothing that already works stops working. Grants are keys in each machine's
`authorized_keys`, enforced by its sshd; fleet is not in the connection path. Members keep
listing, measuring and connecting from what they know.

What waits for the center:

- **changes**: granting and revoking access, removing a machine, invites, and enrolling
  a machine that was added on a member;
- **fresh shared information**: a member sees the others' last readings as of its last
  contact with the center, while still measuring the machines it reaches itself.

When it comes back, pending changes are applied. A member whose center has been quiet for
a week says so in `fleet ls`; that is a note, not an error.

## What runs where

| On any machine | Only on the center |
|---|---|
| `ls`, `show`, `top`, `ssh` | `access --allow`, `access --deny` |
| `add` (a member records it; the center enrols it when it syncs) | `rm` |
| `edit`, `paths`, `setup`, `install`, `update` | `invite` |
| `join`, `sync` (on a member: fetch from the center) | handing the role over |
| reading `access` and `center`; `center --pubkey`; `center --leave` | `center --init`, `center --dissolve` |

On a member, a center-only command refuses and names the machine to run it on.

## The background service

The center listens on port **7373** so members can refresh themselves and new machines
can join. Starting a fleet installs a background service that does this, run as you:

| OS | Service |
|---|---|
| Linux | systemd user unit `fleet-center.service` (lingering is enabled, so it runs without a login) |
| macOS | launchd agent `io.fleet.center`; its output goes to `center-service.log` in fleet's state folder |
| Windows | scheduled task `fleet-center`, with a firewall rule for port 7373 |

`fleet center` says whether it is serving. Where there is no service manager — a
container, most GPU rentals — keep it running yourself: `fleet center --listen` under
tmux, `nohup fleet center --listen &`, or the container's entrypoint.

The listener answers only machines the fleet has pinned, plus holders of a valid invite,
and everything in and out is signed. It is not a shell.

## Handing the role to another machine

The role moves only when the current center hands it over; no machine can promote
itself. The new center needs fleet installed (an [invite](add-machines.md#let-the-machine-join-by-itself)
does that).

```bash
fleet center desktop          # on the current center
fleet center --accept         # then on desktop
```

The first command gives `desktop` access to every machine and delivers it the access list,
signed. The second checks that `desktop` can write to every machine, takes the role and
applies the list. Every member follows by itself: the new center's messages carry the
signed handover, so each one moves its trust across without being asked. The old center
becomes an ordinary member the next time you use it.

Until `--accept`, the old center refuses changes. `fleet center --cancel` on the old
center keeps the role if the new one never accepts.

## If the center is lost

There is no automatic recovery: the price of exactly one machine being able to open
every door is that losing it means setting the fleet up again. Keep a copy of

```bash
fleet center --export
```

somewhere off the machine; it is the access list and the machines' keys, which is what
you need to rebuild. A stolen center's key stays in every machine's `authorized_keys`
until you remove it by hand, because no other machine holds a key that could.

## Leaving and ending

```bash
fleet center --leave          # on a member: take this machine out of the fleet
fleet rm gpu-box              # on the center: remove a machine, and its keys everywhere
fleet center --dissolve       # on the center: take the whole fleet down
```

`--dissolve` removes every key from every machine first, and only then forgets the fleet.
If some machines cannot be reached it stops and says which; `--force` finishes anyway.
**Do not delete fleet's files by hand instead**: that leaves every key in place with
nothing left that can remove them.

A new fleet can be started afterwards with `fleet center --init`.
