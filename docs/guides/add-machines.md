# Adding machines

A machine you add needs **SSH and nothing else** — no agent, no daemon, no fleet. Linux,
macOS and Windows all work. You give fleet the SSH command you already use, and it does
the rest in one step.

| The machine | Say to your agent | Section |
|---|---|---|
| one you can already SSH into | "add `ssh ubuntu@10.0.0.7`" | [Over SSH](#over-ssh) |
| one you reach with a particular key file | "add `ssh -i ~/.ssh/my_key ubuntu@1.2.3.4`" | [Over SSH](#over-ssh) |
| one that only takes a password | "add it", then type the password yourself | [With a password](#with-a-password) |
| a GPU rental (vast.ai, RunPod, AutoDL…) | "add `ssh -p 40001 root@1.2.3.4`, it costs $1.89/hr" | [Rentals](#rentals) |
| one fleet cannot get into from here | "invite my laptop" | [Let the machine join by itself](#let-the-machine-join-by-itself) |
| one that does not exist yet | "give me the key for a cloud-init template" | [Before the machine exists](#before-the-machine-exists) |

## Over SSH

```bash
fleet add "ssh ubuntu@10.0.0.7"
fleet add "ssh -p 2222 -i ~/.ssh/my_key root@1.2.3.4"
fleet add "ssh gpu"                      # a Host from your ~/.ssh/config
```

Anything ssh would use works: your ssh-agent, a key file named with `-i` (any kind — a
`.pem` from a cloud provider, an `id_ed25519`), and a `Host` entry in `~/.ssh/config` with
its user, port, key and `ProxyJump`. In one step fleet then:

1. **connects** with that;
2. **puts the fleet's own key** in the machine's `authorized_keys`, inside a marked block
   it alone edits — your keys and the provider's are left as they are;
3. **gives the machine a fleet key of its own** (in fleet's folder — `~/.config/fleet` on Linux — if it has none)
   and records it: that key is the machine's identity when it is granted access to
   another machine;
4. **measures it**: CPU, memory, GPUs, disks, and what is free right now.

After that fleet reaches the machine with its own key, so other machines you
[grant access](access.md) can reach it too, not only this one.

That is all fleet leaves on the machine: its block in `authorized_keys` and the
machine's key file. No program, no service.

**The machine has to answer.** One that does not is not recorded at all, and fleet says
why: a machine the center cannot reach could not be managed anyway.

### Names, aliases and tags

fleet names the machine after its hostname. To choose:

```bash
fleet add "ssh root@1.2.3.4" --name a100 --alias a --tag training
```

- `--name` — what it is called everywhere.
- `--alias` — a short handle that works wherever a name does: `fleet ssh a`. It can never
  be another machine's name or alias, so it is never ambiguous.
- `--tag` — a label of your own, repeatable; `fleet ls --tag training` finds it again.
  See [Finding the right machine](find-machines.md).

All three can be changed later with `fleet edit`.

Adding a machine fleet already knows — the same box at another address — adds the
address to the existing machine, which keeps its name.

## With a password

If no key gets in, fleet asks for the password **in your terminal**. It is used for one
connection, to put the fleet's key there, and is stored nowhere. An agent never types or
sees it: when a machine needs a password, the agent tells you to run the command
yourself:

```bash
fleet add "ssh admin@192.168.1.50"
```

A Windows center cannot type passwords; see [Windows](windows.md).

## Rentals

Add a rental like any machine, then tell fleet what it costs:

```bash
fleet add "ssh -p 40001 root@1.2.3.4"
fleet edit a100 --cost 1.89
```

fleet recognises vast.ai, RunPod and AutoDL machines as rentals by itself, from what it
finds on them; Lambda and Paperspace only when the address you add by contains
`lambdalabs` or `paperspace`. For any other provider add `--kind rental` to `fleet add`. A rental with a GPU
and none of its GPUs busy gets the alert *RENTAL is idle -- this is costing money*. The
price you set shows in the `$/HR` column and in the fleet's hourly burn rate.

A rental's space is often on a volume such as `/workspace` rather than on `/`. If
`fleet show` reports the wrong disk, name the right one:
`fleet edit a100 --disk-path /workspace` ([more](find-machines.md#disks-tell-fleet-where-the-space-is)).

Rentals change address. When yours comes back on a new IP or port, re-point it without
losing its name, tags, cost or history:

```bash
fleet edit a100 --ssh "ssh -p 40123 root@5.6.7.8"
```

## Let the machine join by itself

When fleet cannot get in from here — no key works, you do not want to type a password, or
the center cannot reach the machine's address — turn it round and let the machine come
to the center. On the center:

> **You:** invite my laptop

or `fleet invite laptop`. For a machine the fleet already knows — one you added with
`fleet add` and now want to run fleet on too — leave the name out: `fleet invite`. A
machine that joins is recognised and keeps the name it has, and an invite for a name
already taken is refused. It prints one line to run on the new machine: `fleet join …`
if fleet is installed there, or the installer with `--join …` if not. Paste it into a
terminal there, or give it to the agent on that machine. The machine installs fleet as a
**member**, dials the center, is admitted, and puts the center's key in its own
`authorized_keys`. No password, and nothing to approve afterwards: the invite *was* the
approval.

- **Single use, 15 minutes** by default (`--ttl 2h` for longer). `fleet invite --list`
  shows what became of recent ones; `--revoke ID` withdraws one.
- **Safe to paste into a provisioning script**: a used code is worth nothing, and the
  secret in it never crosses the network.
- **It trusts that center and no other**: the code names the center's key, so an impostor
  answering at the address is refused.

The center must be listening for joins; it is, if its background service is running
(`fleet center` says). And it still manages the machine over SSH afterwards, so sshd
must be running there and reachable from the center. If the address the center sees is
not the one to use, say which is: `fleet join CODE --ssh "ssh me@10.0.0.5"`.

The code tells the machine where to dial — the center's address in the fleet, or its
hostname. If the new machine cannot reach that, give one it can:
`fleet invite laptop --url http://100.64.0.3:7373/sync` (a Tailscale address, a LAN IP).

## Before the machine exists

For a machine you are about to create — from a cloud console, a template, a cloud-init
file — put the fleet's key on it at creation:

```bash
fleet center --pubkey        # on the center
```

prints the key. Run it on the center: on a member it prints that member's own key, which
is not the one the center uses. It works with nothing reachable, which is the point. Paste it where the
provider asks for an SSH key; once the machine is up, `fleet add "ssh root@ADDRESS"` needs
no password at all.

## Networks: what must reach what

**fleet does not solve connectivity.** It uses whatever route already exists, and it
needs only one: **the center must be able to reach every machine over SSH.** The machines
do not need to reach one another.

| From → to | What it is for | Needed? |
|---|---|---|
| center → every machine, SSH | adding, measuring, placing and removing keys, sharing the inventory | **always** |
| member → center, TCP 7373 | joining with an invite; a member asking for fresh information | for [members](center.md) |
| one machine → another | using a grant you made between them — running work from one on the other | only if you grant it |

**Machines behind different NATs** — at home, in the office, rentals in a datacenter —
cannot reach one another directly. Put them on a mesh network such as
[Tailscale](https://tailscale.com) or [ZeroTier](https://www.zerotier.com) (Nebula,
Netbird and plain WireGuard work too), and add them by their mesh address or name:
"add `ssh ubuntu@100.101.102.103`". fleet needs nothing but an address it can route to,
and recognises mesh addresses (the `mesh` [fact](../reference/facts.md)).

A machine the center cannot reach at all can still [join](#let-the-machine-join-by-itself)
and report what it has, but the center cannot place or remove keys on it until it can.

A member measures the machines it can reach itself. For one it cannot reach, it shows
the center's latest reading instead, when the center has a newer one — so `fleet ls` on
the member still says what the machine has. Running work from that member still needs a
route, since `fleet ssh` connects directly.

**Tailscale SSH and similar** answer ssh themselves and decide who may log in from their
own access rules, not from `authorized_keys`. fleet measures and connects to such a
machine as usual, says so when it is added, and leaves access to it to those rules.

## Machines cloned from one image

VMs and containers made from one image share a machine-id, which is what fleet knows a
machine by. When you add one, fleet checks the machine it already knows under that id,
at its own address: if that one is a different host — another hostname, or booted at
another time — the new one is recorded as a separate machine, with an id of its own
written on it, and fleet says so. If it is the same host, the new address is added to
it. Giving each clone its own machine-id (`systemd-machine-id-setup` after emptying
`/etc/machine-id`) avoids the question altogether.

## Adding from a member

`fleet add` works on any machine in the fleet. On a member it records the machine and
says so; the center enrols it the next time it syncs, because only the center places
keys.

## Removing a machine

On the center:

```bash
fleet rm gpu-box
```

Its keys come off every other machine and the fleet's keys come off it, now. It asks
first, takes only an exact name, alias or id (never a prefix), and an agent leaves it to
you.
