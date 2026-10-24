# Adding machines

A machine you add needs **SSH and nothing else** — no agent, no daemon, no fleet. Linux,
macOS and Windows all work. You give fleet the SSH command you already use, and it starts
watching the machine.

fleet connects exactly as `ssh` does from here: your `~/.ssh/config`, your ssh agent, your
keys, Tailscale SSH, the key on your rental provider's account. So the one rule is
**if `ssh` gets in from here, fleet does too.** When it does not, see
[Reaching your machines](reaching-machines.md).

| The machine | Say to your agent | Section |
|---|---|---|
| one you can already SSH into | "add `ssh ubuntu@10.0.0.7`" | [Over SSH](#over-ssh) |
| one in your ssh config or on your tailnet | "what machines could I add?" | [Machines you can already reach](#machines-you-can-already-reach) |
| a GPU rental (vast.ai, RunPod, AutoDL…) | "add `ssh -p 40001 root@1.2.3.4`, it costs $1.89/hr" | [Rentals](#rentals) |
| one that does not let you in yet | "why won't gpu-box let you in?" | [When it does not let you in](#when-it-does-not-let-you-in) |
| the one you are on | (done for you on first use) | [This machine](#this-machine) |

## Over SSH

```bash
fleet add "ssh ubuntu@10.0.0.7"
fleet add "ssh -p 2222 -i ~/.ssh/my_key root@1.2.3.4"
fleet add "ssh gpu"                      # a Host from your ~/.ssh/config
fleet add gpu                            # the same
```

Anything ssh would use works: your ssh-agent, a key file named with `-i` (any kind — a
`.pem` from a cloud provider, an `id_ed25519`), and a `Host` entry in `~/.ssh/config` with
its user, port, key and `ProxyJump`. fleet then:

1. **connects** with that, in batch mode — it never prompts for a password;
2. **measures it**: CPU, memory, GPUs, disks, what is running and what is free now;
3. **records it** in this machine's list, under a name.

Nothing is written on the machine: no key, no program, no service.

**The machine has to let fleet in.** One that refuses, or does not answer, is not
recorded at all, and fleet says why and what to do. Once it is fixed, run the same
`fleet add` again.

`--dry-run` measures the machine and shows what would be recorded, without recording it.

### Names, aliases and tags

fleet names the machine after the address you gave, or else its hostname, and says so.
To choose:

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

## Machines you can already reach

```bash
fleet ls --discover
```

lists the machines you could add but have not: the `Host` entries of your
`~/.ssh/config`, and the machines on your tailnet when Tailscale runs here. Each one comes
with the command that adds it, `fleet add NAME`. Nothing is contacted until you add it.
Ask your agent "what machines could I add?" and it offers them, rather than adding them
all: a tailnet has phones and printers on it too.
[More](reaching-machines.md#finding-what-you-can-already-reach).

## Rentals

Put your public key on your provider account once (vast.ai, RunPod and AutoDL all have a
page for it; [where](reaching-machines.md#rentals)), so every instance you start lets you
in. Then add a rental with the ssh command the provider shows, and tell fleet what it
costs:

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

## When it does not let you in

```bash
fleet doctor gpu-box
```

says why a machine refuses and gives the exact fix: the line that puts your key on it
(for Linux and macOS, a Windows user, a Windows administrator), your rental provider's
key page, the rule your Tailscale SSH policy needs, or how to start its sshd. It changes
nothing. [Reaching your machines](reaching-machines.md) goes through each case.

At a terminal on Linux or macOS, `fleet add` offers to put your key on a machine that
refuses it, with `ssh-copy-id`: you type the machine's password into ssh once, and fleet
never sees it. fleet itself never asks for a password, and an agent never types one.

## This machine

The machine you run fleet on is recorded by itself on first use, and measured directly,
without ssh. `fleet add --self` records it if it is not (after a `fleet rm`, say).

## Networks

**fleet does not create routes.** It uses whatever route your ssh already has, and needs
only one: **this machine must reach each machine it watches over SSH.** The machines do
not need to reach one another.

**Machines behind different NATs** — at home, in the office, rentals in a datacenter —
cannot reach one another directly. Put them on a mesh network such as
[Tailscale](https://tailscale.com) or [ZeroTier](https://www.zerotier.com) (Nebula,
Netbird and plain WireGuard work too), and add them by their mesh address or name:
"add `ssh ubuntu@100.101.102.103`". fleet needs nothing but an address it can route to,
and recognises mesh addresses (the `mesh` [fact](../reference/facts.md)).

**Tailscale SSH** answers ssh itself and decides who may log in from your tailnet's
policy, not from `authorized_keys`. fleet connects through it like any other ssh; if it
refuses, the fix is a rule in the policy ([how](reaching-machines.md#linux-on-a-tailnet)).

## Several machines running fleet

Every machine that runs fleet keeps its own list, of the machines *its* ssh reaches;
there is no center and nothing to sync. A machine you add on your laptop is not in your
desktop's list. To use one list everywhere, share `machines.yaml` (`fleet paths` shows
where it is) with your dotfiles. To put fleet on another machine so its agents can use it
too, see [Installing and upgrading](install.md#putting-fleet-on-more-machines).

## Machines cloned from one image

VMs and containers made from one image share a machine-id, which is what fleet knows a
machine by. When you add one, fleet checks the machine it already knows under that id,
at its own address: if that one is a different host — another hostname, or booted at
another time — the new one is recorded as a separate machine, with an id of its own
written on it (a `device-id` file in fleet's folder there), and fleet says so. If it is
the same host, the new address is added to it. Giving each clone its own machine-id
(`systemd-machine-id-setup` after emptying `/etc/machine-id`) avoids the question
altogether.

## Removing a machine

```bash
fleet rm gpu-box
```

stops watching it: it comes out of this machine's list, and its readings out of the
cache. **Nothing changes on the machine itself.** It asks first, takes only an exact name,
alias or id (never a prefix), and an agent leaves it to you. `fleet add` brings it back,
with everything recorded about it.
