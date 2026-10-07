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

A change the center could not apply yet — the target is switched off — is shown as
**pending** (a grant) or **revoking** (a revoke), with how long it has waited, and never
reported as done. A revoke still shown as revoking means the key is *still there*. The
listening center tries again by itself, every five minutes or so (less often while the
machine stays off), so a revoke lands soon after the machine is back. `fleet sync` on the
center tries at once.

On a member, `fleet access` names the center; changes have to be made there.

## What access does not cover

- **A key is a key.** A machine holding a grant can use plain `ssh` and bypass fleet
  entirely. The list decides what fleet *places*, not what sshd *allows*.
- **Root on a machine can re-add a key you revoked.** Revoking is not cleaning up a
  compromised machine.
- **A machine the center cannot reach** cannot be granted or revoked anything until it
  can.

The reasoning, and the full threat model, are in [Access (design)](../design/access.md).
