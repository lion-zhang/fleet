# Access: keys the center places, not passwords fleet keeps

Superseded the secrets half of [sync-and-secrets.md](sync-and-secrets.md). The sync half
of that document is still accurate; everything it says about `secrets.age`, age
recipients and `fleet identity` describes a subsystem that no longer exists.

## The one idea

Access is enforced by `authorized_keys` on the target, by an sshd fleet does not run and
is not in the path of. So the access list is **not a permission check** — it is the input
to a reconciler, and only the center runs the reconciler.

The property that actually holds is therefore not "only the center can edit the list" — a
machine can always edit bytes on its own disk — but:

> **Only the center can cause a key to appear in another machine's `authorized_keys`.**

That is structural. Writing that file requires SSH access to it, and after bootstrap only
the center has that everywhere.

## Prerequisite: fleet does not solve NAT

The center must be able to reach every device it manages, because installing or removing
a key means opening a connection to it. A device the center cannot dial cannot be managed
at all — so each one needs a public address, membership of an overlay network, or a
shared LAN.

Fleet is neutral about which overlay. Tailscale, ZeroTier, Nebula, Netbird, Headscale and
hand-rolled WireGuard all work; fleet wants a routable address and nothing more, and
hard-codes none of them. `Endpoint.via` says `mesh`, never a vendor.

This is also why the center dials out rather than being dialled: center→spoke reachability
is required regardless, so carrying sync over it costs nothing, where spokes dialling the
center would need a second guarantee on top.

## Three ways in

Fleet installs **its own** key. What gets it in the first time is a separate question.

1. **Password auth available** — typed once, spent on one connection, discarded.
2. **Password auth off, but a key you already hold works** — the normal case on a cloud
   VM. `build_enroll_argv` omits `IdentitiesOnly=yes` for that first dial so the agent
   can answer; `build_argv` keeps it for everything after.
3. **Neither** — the key must be pre-placed. `fleet center --pubkey` prints it and needs
   nothing reachable, because the moment you want it is before the machine exists.

## What lives where

| File | Who writes it | What it is |
|---|---|---|
| `access.yaml` | center only | Authority. Never travels upward |
| `access-ledger.yaml` | center only | Desired vs observed, retries, last error |
| `access-cache.yaml` | each machine | The center's pinned key, when it last swept |
| `access-outbox.yaml` | each machine | Requests we have filed |

Every name is globally distinct and **nothing is ever deleted by globbing a directory**:
`CONFIG_DIR` and `STATE_DIR` are the same directory on macOS, so tidying up "the state
dir" would take the center's own authority file with it.

The list is not a `Device` field, for three independent reasons. `inventory.merge` is
whole-record last-writer-wins on `updated_at` against unsynchronised clocks, so "I grant
myself everything, timestamped next Tuesday" would win. `_payload` drops falsy values, so
"may reach nothing" could not round-trip. And an edge needs `pending_since`, `attempts`
and `last_error` to say a revoke has *not* landed yet, which is the difference between
this and a note to self.

## Identity

Edges are keyed on the **SHA256 fingerprint of a machine's fleet key**, not its device id.
Device ids are not stable: `derive_id` returns `net:<target>:<port>` whenever a probe
fails — exactly the awaiting-enrollment case this design creates — and `edit` rewrites it
afterwards. A fingerprint is stable because the key *is* the identity.

`Device.pubkey` is published and informational. The center pins the binding it read over
its own connection, because a field that rides the merge can be overwritten by a peer with
a fast clock — after which the center would install that peer's key where the owner's
belonged.

## Marker blocks

```
# fleet:7f3a9c:begin from=SHA256:... user=root
ssh-ed25519 AAAA... fleet:7f3a9c:machine_B
# fleet:7f3a9c:end from=SHA256:...
```

Only lines inside a block carrying our own fleet id are ever touched. The user's key, a
provider's injected key and another fleet's block all survive. The file is written beside
and renamed, so a dropped connection leaves the old one intact. A block whose end marker
is missing does not cause a skip to EOF — any other fleet marker ends it — because that
would delete every key below ours.

Windows needs a twin, not an adaptation. Both of its failure modes are silent: the
`Match Group administrators` rule means appending to `~/.ssh/authorized_keys` for most
Windows logins is simply ignored, and sshd refuses a file with inherited ACEs without
saying so. A Windows grant is therefore only ever confirmed by connecting on the new key.

## Signing

The center dials a spoke and runs `fleet sync --serve` **there** — but a grant *is* a key
on that spoke, so any granted peer can reach it and run the same filter. SSH proves *a*
peer, not *the* center.

So the whole sync envelope is signed (SSHSIG, over the fleet key we already have) and
verified before anything is merged. Not just the access list: the inventory carries the
endpoints that decide where `fleet ssh` dials, and `inventory.merge` unions endpoints with
no timestamp contest and no deletion primitive, so an injected low-preference route would
win everywhere and could never be removed.

First contact pins the center's key — the same bargain ssh makes with host keys, for the
same reason. An unsigned payload is refused outright rather than accepted as a legacy
format, because "old peer" and "hostile peer" are indistinguishable from the receiving end.

## The center is expected to be offline

Nothing that already works stops working when the center is closed: grants are keys in
`authorized_keys`, enforced by sshd, with fleet nowhere in the connection path. Queued
grants and revokes drain when it returns, because the ledger is desired-vs-observed rather
than a work queue.

A stale cache **warns and proceeds, never denies**. A center off for a fortnight is a
laptop on holiday, and treating that as revocation would turn a sync outage into a fleet
outage.

## Who decides

Only the current center can name the next one. No machine may promote itself, under any
condition. A self-promotion path is a hostile-takeover primitive whose guards are
themselves security-critical code; removing the path removes the attack. `fleet edit
--role center` and `fleet install --role center` both used to do exactly that and now
refuse.

There is no `backup` role. It meant a second machine holding a key on every device
forever — a standing total-compromise target, to save an occasional manual recovery.

So an unplanned loss of the center means re-configuring by hand. That is the accepted
price of there being exactly one machine that can open every door. Hand over before you
retire a machine, and keep `fleet center --export`.

## Threat model, plainly

**Protects against** a compromised machine reaching one it was never granted (sshd
enforces it, not fleet); revocation being a note to self; passwords at rest; clock-skew
escalation, since the list never merges and keys are pinned; and a peer impersonating the
center, since it cannot sign.

**Does not protect against** a machine holding a key bypassing fleet entirely — `ssh -i
~/.config/fleet/id_ed25519 root@B` works regardless of the list, so any design where
`fleet ssh` is the enforcement point is theatre. **Center compromise is total compromise**,
for exactly one machine. Root on a target can re-add a revoked key. Revoke is not
remediation. And a device the center cannot reach cannot be managed at all.

A stolen center is worth stating separately: its key stays in every `authorized_keys`
until you visit each machine, because the replacement center has no key anywhere to remove
it with.
