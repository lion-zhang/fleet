# Access: keys the center places, not passwords fleet keeps

fleet never stores a password. The center places an SSH key once, and from then on
there is no credential left to keep. This page is how that works and what it does and
does not protect. How machines share the inventory is in [sync.md](sync.md).

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

This is also why the center dials out rather than being dialled: center→member reachability
is required regardless, so carrying sync over it costs nothing, where members dialling the
center would need a second guarantee on top.

## Ways in

Fleet installs **its own** key. What gets it in the first time is a separate question,
and enrolment (`ops/enrol.py`) tries these in the order that asks least of the user:

1. **A key you already hold works** — the normal case on a cloud VM, where password auth
   is off and the provider injected a key at creation. `build_enroll_argv` omits
   `IdentitiesOnly=yes` for that first dial so the agent can answer, and carries
   `BatchMode=yes` so it can never block on a prompt; `build_argv` keeps IdentitiesOnly
   for everything after. Trying this first is what lets an agent enrol unattended.
2. **Password auth available** — typed once, spent on one connection, discarded. Needs a
   human, so it needs a terminal (and a pty, which Windows does not have).
3. **Neither** — the key must be pre-placed. `fleet center --pubkey` prints it and needs
   nothing reachable, because the moment you want it is before the machine exists.
4. **The machine joins** — `fleet invite` on the center prints a code, and `fleet join
   CODE` on the machine dials the listener with it. The machine writes the center's key
   into its own `authorized_keys`, so nothing is installed from outside and no password
   exists anywhere. See *Invites* below.

Enrolment is not a command. `fleet add` does it on the center, and the sweep does it for
anything still unpinned — a machine added from a member, or one whose enrolment was
interrupted. Neither ever prompts outside case 2, and the sweep never prompts at all.

## What lives where

| File | Who writes it | What it is |
|---|---|---|
| `access.yaml` | center only | Authority. Never travels upward |
| `access-ledger.yaml` | center only | Desired vs observed, retries, last error |
| `access-cache.yaml` | each machine | The center's pinned key, when it last swept |
| `access-invites.yaml` | center only | Open invites, as hashes of their secrets |
| `access-chain.yaml` | a center that took the role | Every signed handover from the first center to this one |
| `access-handover-in.yaml` | the successor, until `--accept` | The handover the outgoing center delivered |
| `access-handover-out.yaml` | the outgoing center, until settled | Who it named, and where to ask whether they took it |

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

The center dials a member and runs `fleet sync --serve` **there** — but a grant *is* a key
on that member, so any granted peer can reach it and run the same filter. SSH proves *a*
peer, not *the* center.

So the whole sync envelope is signed (SSHSIG, over the fleet key we already have) and
verified before anything is merged. Not just the access list: the inventory carries the
endpoints that decide where `fleet ssh` dials, and `inventory.merge` unions endpoints with
no timestamp contest and no deletion primitive, so an injected low-preference route would
win everywhere and could never be removed.

First contact pins the center's key — the same bargain ssh makes with host keys, for the
same reason. An unsigned payload is refused outright rather than accepted as a legacy
format, because "old peer" and "hostile peer" are indistinguishable from the receiving end.

## Invites

The listener refuses any key it has not pinned, because answering a stranger would let
the earliest caller pin itself. An invite is how a stranger is admitted without
weakening that: it is the center speaking first, just earlier, and out of band.

- **Single use, minutes long.** The first key to redeem it owns it. The same key asking
  again is accepted — that is a lost reply, not a second machine — and any other key is
  refused. Everything that can fail is checked before the invite is spent, so a refused
  join costs nothing.
- **The secret never travels.** The request carries a MAC over the joiner's own sealed
  envelope, keyed by the invite. The listener speaks plain HTTP, and watching it yields a
  MAC bound to one key's request, which is useless for any other key.
- **No trust on first use on the joiner's side.** The code carries the fingerprint of the
  center's key, and the joiner accepts a reply signed by exactly that key or nothing.
  Ordinary sync has to pin whoever answered first; a join does not.
- **The joiner describes itself, not the fleet.** The center records one machine from a
  join — no role, no other records, no identity paths that only mean something on the
  joiner — under a name the invite chose or that is made unique. A machine-id that is
  already pinned to another key is refused, as `enroll` refuses everywhere.
- **Stored as a hash.** `access-invites.yaml` holds sha256 of each secret, which is also
  the MAC key. Reading it grants what the invite grants, for minutes, on the one machine
  whose compromise is total anyway.

What an invite does not change: the center still manages the machine over ssh, so a
machine it cannot reach can join and report, but grants on it wait until it can be
reached. Grants a machine applies to itself from pulled, signed data would lift that;
they are not built.

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
--role center` and `fleet install --role center` refuse.

There is no `backup` role. It meant a second machine holding a key on every device
forever — a standing total-compromise target, to save an occasional manual recovery.

A handover moves the role in two signed steps, and the fleet follows by itself:

1. **On the center, `fleet center NAME`.** Grants the successor every machine, as the
   user each is reached as, and applies those grants now. Then delivers over ssh the
   access list, the ledger and a handover record naming the successor's key -- the
   bundle signed by the outgoing center, which the successor has pinned and checks.
2. **On the successor, `fleet center --accept`.** Proves it can *write* every machine
   (probing would only prove its key is present), takes the role, and sweeps.
3. **Everyone else follows the chain.** Every envelope the new center signs carries the
   handover records, oldest first. A member that still trusts an older key walks from it
   one record at a time, each signed by the key it hands *from*, and pins where the walk
   ends. A forged record leads nowhere, so this is never trust on first use.
4. **The outgoing center steps down on proof.** The next time it acts as center it asks
   the successor; a reply signed by the successor that carries the record it signed is
   the proof, and it becomes a member. Its key comes off every machine on the new
   center's first sweep, because the new list no longer wants it.

A member the new center cannot reach learns of the handover only by asking: `fleet sync
--from` the new center's address once.

An unplanned loss of the center means re-configuring by hand. That is the accepted
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
