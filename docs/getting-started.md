# Getting started

You do the setup once. After that you talk to your coding agents, and they run fleet for
you — you come back only to grant access, or when something needs a password.

Three commands, about ten minutes.

---

## Before you start

**The center must be able to reach every machine it manages.** Installing or removing a
key means opening an SSH connection to it, so a machine the center cannot dial cannot be
managed at all. Each one needs a public address, membership of an overlay network, or a
shared LAN with the center.

fleet does not care which overlay — Tailscale, ZeroTier, Nebula, Netbird, WireGuard all
work. It wants a routable address and nothing more. If your machines are already on one,
use it; that removes this whole class of problem.

**Machines you only connect *to* need nothing installed.** The probe is one script piped
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

## 2. Create the fleet

Do this **first**. A machine is added *to* a fleet, so the fleet has to exist — `fleet
add` refuses on a machine that is in none, and says so.

```bash
fleet center --init
```

The machine you run this on is now **the center**: the one that decides who may reach
what, and the only one that can install or remove a key. Pick the machine you actually
work from. A laptop is fine, and being closed half the day is expected.

**The center stays the center.** There is no self-promotion and no election — the only
way the role moves is `fleet center machine_B`, run on the current center, which installs
the successor's key everywhere and verifies it can write before retiring the old one. If
the center is lost outright, you rebuild the fleet by hand; `fleet center --export` is
worth keeping somewhere for that day.

## 3. Add your machines

One pasted SSH command each — whatever you already use to reach them.

```bash
fleet add "ssh username@host.example.com"
fleet add "ssh -p 58418 root@1.2.3.4" --name machine_A --alias a
fleet ls
```

Names are guessed from the host; `--name` overrides that, and `--alias` gives the machine
a short handle you can type anywhere a name goes — `fleet ssh a`, `fleet show a`, `fleet
access a --allow b`. Both are optional, and `fleet edit NAME --alias SHORT` sets one later
(`--alias ""` removes it). An alias cannot be another machine's name or alias: the point
is that the short form is never ambiguous.

Adding is enrolling. In one step fleet probes the machine, puts its key there, reads back
a key of the machine's own and pins it, and records the lot. There is no second command.

**The machine has to answer.** One that does not is not recorded at all, and fleet says
why. That is deliberate: a machine the center cannot reach cannot be granted or revoked
anything, so recording it would only produce an entry that fails the first time anyone
uses it.

Three ways fleet gets in, and it works out which:

- **A key you already hold works** — the usual case on a cloud VM, where password auth is
  off and the provider injected a key at creation. Nothing is asked of you.
- **The host takes a password** — you type it once. It is spent on one connection and
  stored nowhere.
- **Neither** — put the center's key on the machine yourself. `fleet center --pubkey`
  prints it, and works with nothing reachable and before the machine exists, which is the
  point: it is what you paste into a provisioning template, a cloud-init file, or the
  provider's console. Then add the machine, and no password is needed at all.

`fleet add` is **not** center-only. Run it anywhere, and on a machine that is not the
center it records the machine and says so — the center picks it up and enrols it on its
next `fleet sync`, because only the center can write the access list.

## 4. Hand it to your agents

```bash
fleet setup
```

Writes instructions for whichever of Claude Code, Codex and Hermes are actually installed
— a skill file where the agent owns the directory, and a marked region in `AGENTS.md`
where you own the file, which leaves the rest of it alone. It will not create a config
directory for an agent you do not use. Re-run it after upgrading fleet.

---

## Talking to your agents

This is the point the setup hands over. You ask in words; the agent picks the command.

| You say | The agent runs |
|---|---|
| "what's free right now?" | `fleet ls --json` |
| "train this on whatever has a spare GPU" | `fleet ls --json`, then `fleet ssh NAME -- ...` |
| "add my new rental, ssh -p 40001 root@1.2.3.4" | `fleet add "ssh -p 40001 root@1.2.3.4"` |
| "let machine_B reach machine_A" | `fleet access machine_A --allow machine_B`, then `fleet sync` |
| "is machine_A usable yet?" | `fleet access` — reads the pending rows rather than guessing |
| "what's costing me money?" | `fleet ls --json` and reads the alerts |

**Expect to be asked back.** Agents are told to ask rather than guess when a request does
not carry everything a command needs — which machine, whose `authorized_keys`, the whole
ssh command. That is deliberate: `fleet rm`, `fleet access --deny` and `fleet center
--dissolve` are not undone by running them again, and a machine name resolved from a
half-heard fragment is how the wrong one gets removed.

> **You:** give machine_B access
> **Agent:** To which machine, and as which user? A host often answers as both `root@`
> and `ubuntu@`, and the grant is per-user.

**What does not go through an agent.** Typing a password — only `fleet add` on the center
ever asks, and a human has to run it. And the irreversible ones: `fleet rm`, handing the
center over, `fleet center --dissolve`. Agents are told to report a refusal rather than
work around it.

## Granting access

By default machines cannot reach each other; only the center can reach everything.

```bash
fleet access machine_A --allow machine_B   # let machine_B reach machine_A
fleet sync                                 # apply it
fleet access                               # who may reach what, and what is pending
```

`fleet access` records the decision; `fleet sync` installs the key. Until the sweep
reaches the machine the grant shows as pending, with an age — never as done. Revoking is
the same shape and the same honesty:

```bash
fleet access machine_A --deny machine_B
fleet sync
```

If the machine is switched off, its key is still on it, and fleet says so rather than
reporting success. It retries on the next sync.

## What needs the center

Most things do not. The split matters because it decides whether something happens now or
waits.

**Anywhere:** `ls`, `show`, `top`, `ssh`, `add`, `edit`, `install`, `update`, `paths`,
`setup`, `center --pubkey`, and reading `access`. Also `fleet center --leave`, which takes
this machine out of a fleet and needs nobody's permission — you own the machine you are
standing on.

**Only on the center:** `access --allow` and `--deny`, `sync`, `rm`, handing the role
over, and `center --init` / `--dissolve`. On any other machine these refuse and say which
machine to run them on.

Nothing already granted stops working when the center is off. Access is enforced by sshd
reading `authorized_keys`, and fleet is not in the connection path — which is also why a
machine holding a key can bypass fleet and use plain `ssh`. The list governs what fleet
*does*, not what SSH *allows*.

## Ending things

```bash
fleet center --leave         # take this machine out of a fleet
fleet rm machine_A           # the center removes a machine, revoking its keys
fleet center --dissolve      # take the whole fleet down: every key off every machine
```

**Do not delete `access.yaml` by hand.** That does not dissolve a fleet, it orphans one:
the center can no longer manage anything and every machine keeps its keys with nothing
able to remove them. `--dissolve` removes the keys *first*, and only then forgets the
fleet.

## When something does not work

| What you see | What it means |
|---|---|
| `This machine is not in a fleet` | Run `fleet center --init` first. The fleet comes before the machines. |
| `<name> did not answer` | Nothing was recorded. Fix reachability and add it again. |
| `auth_failed` | The host is up and refused our key. Only the center can install one. |
| A grant that stays `pending` | The sweep has not reached that machine. Not a failure. |
| `the center has not swept this machine for N days` | Normal. Everything already granted keeps working; only *changes* wait. |
| `no access list ... not a center` | You are on a spoke. Run the command on the center. |
| `No machine named exactly ...` | `fleet rm` will not act on a prefix. Give the full name. |

## Windows

Windows machines work as targets with nothing to configure. They are probed and keyed
over PowerShell, and `fleet ssh machine_A -- cmd` passes the command through rather than
wrapping it in a shell that does not exist there. fleet works this out from the last
probe; you never declare it.

Install OpenSSH Server yourself first — fleet does not set it up. Running fleet *on*
Windows is not supported yet.

---

## Appendix: the commands worth knowing

You will not need most of these; the agent will. `fleet <command> --help` carries an
example for every one.

**Looking around**

```bash
fleet ls                       # every machine, with what is free right now
fleet ls --online              # only the reachable ones
fleet ls machine_A -r          # just this one, freshly probed
fleet show                     # this machine in detail; `fleet show NAME` for another
fleet top                      # live view; needs a terminal
```

**Connecting**

```bash
fleet ssh machine_A                    # a shell, exactly as plain ssh
fleet ssh machine_A -- nvidia-smi      # run one command there
```

**Changing the fleet** — the first two anywhere, the rest on the center

```bash
fleet add "ssh user@host"              # add and enrol a machine
fleet add "ssh user@host" --name machine_A --alias a     # naming it yourself
fleet edit machine_A --ssh "ssh -p 40001 root@1.2.3.4"   # it moved
fleet edit machine_A --alias a                           # a short handle to type
fleet edit machine_A --disk-path /workspace              # watch this volume
fleet access machine_A --allow machine_B                 # grant
fleet access machine_A --deny machine_B                  # revoke
fleet sync                             # apply everything, and enrol anything pending
fleet rm machine_A                     # remove, revoking its keys. Exact name only
```

**The center itself**

```bash
fleet center                   # who decides, and when it was last heard from
fleet center --pubkey          # the key to pre-place on a host
fleet center --export          # the access list and pins, worth keeping off the machine
fleet center machine_B         # hand the role over; then `fleet center --accept` there
fleet center --leave           # take this machine out of the fleet
fleet center --dissolve        # take the whole fleet down
```

**Housekeeping**

```bash
fleet install machine_A        # put fleet on a machine that has none
fleet update --all             # deploy the newest fleet everywhere
fleet setup                    # re-teach your agents after an upgrade
fleet paths                    # where the inventory, keys and access list live
```

---

Design and rationale: [design/access.md](design/access.md).
