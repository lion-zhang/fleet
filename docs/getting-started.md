# Getting started

You do the setup once. After that your coding agents can use the fleet on their own, and
you only come back to grant or revoke access.

Budget about ten minutes for a handful of machines.

---

## Before you start

**fleet must be able to reach every machine it manages.** Installing or removing a key
means opening an SSH connection to it, so a machine fleet cannot dial cannot be managed
at all. Each one needs a public address, membership of an overlay network, or a shared
LAN with the machine you make the center.

fleet does not care which overlay — Tailscale, ZeroTier, Nebula, Netbird, WireGuard all
work. It wants a routable address and nothing more. If your machines are already on one,
use it; that removes this whole class of problem.

**Machines you only connect *to* need nothing installed.** The probe is a script piped
over one SSH connection. Only machines that run fleet commands themselves need fleet.

---

## 1. Install

fleet is not on PyPI; it installs from the repo.

```bash
git clone git@github.com:lion-zhang/fleet.git
uv tool install ./fleet
fleet --version
```

Later, `fleet update` re-runs exactly this on any machine that already has it.

## 2. Add your machines

One pasted SSH command each — whatever you already use to reach them.

```bash
fleet add "ssh username@host.example.com"
fleet add "ssh -p 58418 root@1.2.3.4"       # a rental
fleet add --self                            # the machine you are on, no SSH needed
fleet ls
```

`fleet add` probes immediately and records the machine either way. A host that does not
answer is still listed, with the reason — silently dropping it would be worse.

Names are guessed from the host and can be changed: `fleet edit oldname --name newname`.

## 3. Make one machine the center

The center is the machine that decides who may reach what. It is the only one that can
install or remove keys, so pick the one you actually work from — a laptop is fine, and
being closed half the day is expected.

```bash
fleet center --init
fleet center                 # who decides, and how the fleet looks
```

## 4. Enrol each machine

Enrolling puts the center's key on a machine and gives that machine a key of its own.
Run it from the center, once per machine:

```bash
fleet center --enroll machine_A
```

Three ways it gets in, and it works out which:

- **The host takes a password** — you type it once. It is spent on one connection and
  never stored anywhere.
- **Password auth is off but a key you already hold works** — the usual case on a cloud
  VM. Nothing is asked of you.
- **Neither** — the key has to be placed in advance. `fleet center --pubkey` prints it,
  and works with nothing reachable, which is the point: you want it when writing a
  cloud-init file or a provisioning template, before the machine exists.

## 5. Grant access

By default machines cannot reach each other; only the center can reach everything.

```bash
fleet access machine_A --allow machine_B   # let machine_B reach machine_A
fleet sync                                 # apply it
fleet access                               # who may reach what, and what is still pending
```

`fleet access` records the decision; `fleet sync` installs the key. Until the sweep
reaches a machine the grant shows as pending, with an age — never as done.

Revoking is the same shape and the same honesty:

```bash
fleet access machine_A --deny machine_B
fleet sync
```

If the machine is switched off, the key is still on it, and fleet says so rather than
reporting success. It retries on the next sync.

## 6. Hand it to your agents

```bash
fleet setup
```

Writes instructions for whichever of Claude Code, Codex and Hermes are actually
installed — a skill file where the agent owns the directory, and a marked region in
`AGENTS.md` where you own the file, which leaves the rest of it alone. It will not create
a config directory for an agent you do not use. Re-run it after upgrading fleet.

---

## Day to day

Mostly you will not touch fleet at all; the agent will. When you do:

```bash
fleet ls                     # what is free right now
fleet ls --online            # only what is reachable
fleet show                   # this machine in detail; `fleet show NAME` for another
fleet top                    # live view, like htop for the fleet
fleet ssh machine_A          # a shell, exactly as plain ssh
fleet ssh machine_A -- nvidia-smi
fleet update --all           # deploy the newest fleet everywhere
```

Where a machine name is the obvious subject, it can be left out: `fleet show`, `fleet
edit`, `fleet update` and `fleet probe` all default to the machine you are on. `fleet rm`
deliberately does not — a command that deletes something should never guess what.

## What agents do, and what stays yours

Agents can read the fleet and use what they have been granted: list machines, inspect
one, run work over `fleet ssh`, see who may reach what. On the center they can also grant
and sync.

They are told never to type a password — the only command that asks needs a human — and
to report a refusal rather than work around it.

Four things stay with you because they are irreversible or need a credential:
`--enroll`, handing the center over, `fleet rm`, and `fleet center --dissolve`.

## Ending things

```bash
fleet center --leave         # take this machine out of a fleet. Needs nobody's permission
fleet rm NAME                # the center removes a machine, revoking its keys
fleet center --dissolve      # take the whole fleet down: every key off every machine
```

**Do not delete `access.yaml` by hand.** That does not dissolve a fleet, it orphans one:
the center can no longer manage anything and every machine keeps its keys with nothing
able to remove them. `--dissolve` removes the keys *first*, and only then forgets the
fleet.

## When something does not work

| What you see | What it means |
|---|---|
| `auth_failed` | The host is up and refused our key. Only the center can install one. |
| `host is Windows: no POSIX shell` | fleet retried in PowerShell and that failed too — check OpenSSH's `DefaultShell` on the host. |
| A grant that stays `pending` | The sweep has not reached that machine. Not a failure. |
| `the center has not swept this machine for N days` | Normal. Everything already granted keeps working; only *changes* wait. |
| `no access list ... not a center` | You are on a spoke. Run the command on the center. |

Nothing already granted stops working when the center is off. Access is enforced by sshd
from `authorized_keys`, and fleet is not in the connection path — which is also why a
machine holding a key can always bypass fleet and use `ssh` directly. The list governs
what fleet *does*, not what SSH *allows*.

## Windows

Windows machines work as targets with nothing to configure. They are probed and keyed
over PowerShell, and `fleet ssh machine_A -- cmd` passes the command through rather than
wrapping it in a shell that does not exist there. fleet works this out from the last
probe; you never declare it.

Install OpenSSH Server yourself first — fleet does not set it up. Running fleet *on*
Windows is not supported yet.

---

Design and rationale: [design/access.md](design/access.md).
