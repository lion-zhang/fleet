# Access between machines

By default **only the center can reach the machines in the fleet**. Machines cannot reach
one another until you say so. Granting access puts one machine's key on another;
revoking takes it off. Only the center can do either.

> **You:** let the laptop reach the NAS
>
> **Agent:** Done — `laptop` can now reach `nas` as `admin`, the account fleet reaches it as.

## Granting

```bash
fleet access nas --allow laptop                 # laptop may reach nas
fleet access gpu-box --allow laptop --user ubuntu
```

The grant is applied on the spot: the center connects to the target and adds the key.
There is no second command. A grant is **per user** — a machine often answers as both
`root` and `ubuntu`, and each is a separate grant. Without `--user`, the grant is for
the account the center itself reaches that machine as (`root` if it has none), which is
why the agent asks which one you mean when it is not clear.

After that, on the laptop, `fleet ssh nas` works, and so does plain `ssh` with the
laptop's fleet key: access is enforced by the target's sshd, not by fleet.

## Revoking

```bash
fleet access nas --deny laptop                  # every account laptop was granted on nas
fleet access nas --deny laptop --user ubuntu    # just this one
```

Revoking is pushed to the target at once, rather than waiting for it to check in: a
machine that waited to be told would keep the key until it next happened to sync, which
for an idle machine might be never. Run it again any time; it is safe to repeat.

## What is in effect

```bash
fleet access            # every grant, and anything still pending
fleet access nas        # just this machine
```

Each row's state is one of three words:

| State | Means |
|---|---|
| **present** | the key is on the machine, as the list says |
| **pending** | a grant the center could not place yet — the machine was off |
| **revoking** | a revoke the center could not apply yet: the key is *still there* |

A pending or revoking row shows how long it has waited and how many attempts it took,
and is never reported as done. The
listening center tries again by itself, every five minutes or so (less often while the
machine stays off), so a revoke lands soon after the machine is back. `fleet sync` on the
center tries at once. A row whose error stays the same attempt after attempt — an account
that does not exist there, a wrong `--user` — will not land by waiting: fix the cause, or
revoke it.

On a member, `fleet access` names the center; changes have to be made there.

## Keys nothing manages any more

```bash
fleet access --prune              # list what is stale on every machine, then ask
fleet access nas --prune --yes    # just nas, without asking
```

`--deny` removes a key the list knows about. Some keys outlive the list's memory of
them: a machine reinstalled with a new key, a fleet dissolved with `--force` while a
machine was off, a machine removed while it could not be reached. They stay in
`authorized_keys` until something takes them off — and every block fleet writes says
which fleet put it there and whose key it is, so the center can ask each machine and
compare. `--prune` reads every machine it can reach and removes the blocks of this fleet
that the list no longer wants.

What it keeps, and why:

- **This fleet's block for a key is the copy that stays.** It is the one `--deny` can
  find later.
- **Another fleet's blocks** are listed but kept, since that fleet may still be running;
  `--fleet ID` removes the ones a fleet you no longer run left behind.
- **The last copy of a key the list still wants** is never removed. If this fleet's own
  block for it is not there yet — a grant still pending, or the center's own key on a
  machine an old fleet set up — the old block is what lets it in, and it stays until
  this fleet's block is in place beside it.
- **A key outside any fleet block** is never touched, since fleet does not edit lines it
  did not write. But a key the fleet manages that also sits there bare is reported: that
  copy would keep working after a revoke. Delete it by hand if fleet should be the one in
  charge of that key.

Machines it could not read are named, so nothing is reported clean that was not looked at.

## A machine was reinstalled

A reinstalled machine — or one whose fleet folder was deleted — gets a new fleet key,
and every grant names a key, not a machine. So:

- **Rejoining moves its grants, if you invite it by name.** `fleet invite NAME` on the
  center, then `fleet join CODE` on the machine: the center recognises the machine, moves
  its grants to the new key and puts them back in place on the next sweep. With an
  invite that does not name it, the join is refused and says how many grants depend on
  the old key — a changed key is never taken quietly.
- **`fleet add` does not do this.** It reports that the machine already has a different
  key pinned and stops there; use the invite.
- **Either way, the old key's blocks stay on the machines it was granted** until
  `fleet access --prune` removes them. Re-granting does not.

Until the grants are back, reaching another machine from the reinstalled one fails with
`Permission denied (publickey…)` — a missing grant for its new key, not a broken target.

## What access does not cover

- **A key is a key.** A machine holding a grant can use plain `ssh` and bypass fleet
  entirely. The list decides what fleet *places*, not what sshd *allows*.
- **Root on a machine can re-add a key you revoked.** Revoking is not cleaning up a
  compromised machine.
- **A machine the center cannot reach** cannot be granted or revoked anything until it
  can.

The reasoning, and the full threat model, are in [Access (design)](../design/access.md).
