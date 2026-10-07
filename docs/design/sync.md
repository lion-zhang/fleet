# Sync

How the machines in a fleet share what they know, and why the center is allowed to be
offline. For the access list and keys, see [access.md](access.md).

## What is shared

| File | Shared | Why |
|---|---|---|
| `inventory.yaml` | yes, with every machine | the machines, their addresses, names, tags and cost — the thing you curate |
| latest readings | relayed by the center | so a member sees machines it cannot measure itself |
| `cache.db` | no | disposable; any machine can rebuild it with one reading |
| `access.yaml` | never upward | the center's authority; see [access.md](access.md) |
| the fleet key | never | a machine's private key does not leave it |

## Two directions

**The center pushes.** `fleet sync` on the center runs the *sweep*: it brings every
machine's `authorized_keys` in line with the access list, measures the machines it
touched, then hands the signed inventory to every machine it can reach, by running
`fleet sync --serve` there over ssh. A member that runs fleet merges what it receives and
answers with its own copy.

**Members pull.** A member asks the center for fresh information when it needs it: on
`fleet ls` or `fleet show`, if it last heard from the center more than `sync_ttl_s` ago
(five minutes). It posts its signed inventory and readings to the center's listener
(`POST /sync` on port 7373) and merges the signed answer. This is lazy on purpose: a
machine nobody is using needs no fresh data, and the moment someone uses it, it gets
some. Only one process per machine asks at a time; the others carry on with what they
have rather than wait. A center that does not answer is asked less often — a minute,
doubling per miss, up to `offline_backoff_max_s`.

A machine the center cannot reach can still pull, once it knows where the center is:
`fleet sync --from URL` (or joining with an invite, which records it).

## Merging

Records are matched on the machine's id, which prefers its machine-id over an address —
two machines may have named the same box differently, and its address may have changed.

1. **Known to one side only** — kept.
2. **Known to both** — the record with the newer `updated_at` wins *whole*. Not field by
   field: merging fields independently could assemble a machine that never existed on
   either side, and there is no per-field timestamp to justify it. An older record adds
   nothing, not even an address.
3. **Addresses** — when the newer record wins, or both are equally new, the two lists
   are unioned, so a box one machine reaches over the LAN and another over Tailscale is
   one box with two routes. Except when the center's signed inventory is merged — its
   answer to a member, or what it pushes after a sweep: then its list replaces ours, so a
   wrong route can be removed by editing it on the center.

**What the center takes from a member.** A member's upload is filtered before it is
merged. About itself, a member may change anything but its role. About any other
machine it may change only the tags, cost, disk paths and notes; its name, addresses,
kind and role stay as the center has them, and a machine the center does not know yet
comes in with no role. A member's timestamps are capped at the center's clock, so a
member whose clock runs ahead cannot win every merge for days.

**Deletion is a tombstone.** `fleet rm` keeps the record with `deleted_at` set, and the
newer-wins rule carries it like any other change; a record that merely vanished would be
indistinguishable from one the other side has not seen yet, and would come back on the
next sync. A member's tombstone is accepted only for itself. Tombstones are pruned after
a year, long after every machine has seen them. Adding the machine again later makes
a newer record, so it comes back rather than staying deleted.

## Signing

Everything that crosses between machines — in both directions — is signed with the
sender's fleet key (SSHSIG) and checked before anything is merged. The inventory decides
where `fleet ssh` connects, and addresses are unioned without any timestamp contest, so
an unsigned inventory would let anyone who could reach a machine add a route that wins
everywhere.

The listener accepts only machines the fleet has already pinned — never a stranger on
first contact — and a member accepts answers only from the center's pinned key, or from a
successor reached through a signed [handover](access.md#who-decides). The only way in for
a new machine is enrolment by the center, or an [invite](access.md#invites).

## The center is expected to be offline

Every command works from local state. A center that cannot be reached costs freshness and
nothing else: members list, measure and connect from what they have, and a failed refresh
is silent. A member whose center has been quiet for a week says so in `fleet ls` — a
note, never a refusal. Treating a quiet center as a reason to deny would turn a laptop on
holiday into a fleet outage.
