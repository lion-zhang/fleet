# Getting started

This walks you through your first fleet: one machine to run it from and one machine
added to it, then a first job on that machine. It takes about ten minutes. You talk to
your coding agent the whole way. The command behind each step is shown too, for when you
would rather type it yourself.

You need:

- a coding agent that can run commands — Claude Code, Codex, Gemini CLI, Copilot CLI,
  OpenCode or [any other](guides/agents.md);
- one more machine you can already reach with `ssh` from the first one: a server, a
  desktop, a cloud VM or a GPU rental. Nothing has to be installed on it. If the two are
  behind different NATs, put them on a mesh network such as Tailscale or ZeroTier first
  ([why](guides/add-machines.md#networks-what-must-reach-what)).

## 1. Choose the center

The machine you install fleet on becomes your fleet's **center**. It keeps the list of
your machines, holds the key that reaches them, and decides which machine may reach which.

The machines you add to it need nothing installed — only SSH. You install fleet on a
second machine only if you also run agents there, or want to check the fleet from it;
that machine is then a **member** ([step 6](#6-optional-a-machine-that-runs-fleet-too)).

Pick a machine that can reach all your devices and is online most of the time — a desktop
or a home server is ideal. A laptop works too: while it is closed, everything already set
up keeps working, and only changes wait for it. You can move the role later
([how](guides/center.md#handing-the-role-to-another-machine)).

## 2. Install fleet

On the center, ask your agent:

```text
Install fleet from https://github.com/lion-zhang/fleet
```

The agent runs the installer. It installs the `fleet` command, starts a new fleet with
this machine as its center, and teaches every supported agent on this machine to use it —
not only the one you asked. It ends by listing what to try next.

<details>
<summary>Installing it yourself</summary>

```bash
curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh
```

On Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"
```

Options, other package managers and upgrading: [Installing and upgrading](guides/install.md).
</details>

## 3. Look at the center

> **You:** show me this machine

The agent runs `fleet show` and describes what fleet measured: CPU and load, memory, GPUs
and their free VRAM, disks, the busiest processes, the services listening on ports, and
the *facts* agents use to pick a machine (`linux`, `cuda`, `vram-24g`, …). Every agent on
this machine now has this view of every machine in the fleet.

## 4. Add a machine

Give the agent the SSH command you already use for the other machine:

> **You:** add `ssh ubuntu@10.0.0.7`
>
> **Agent:** Added as `gpu-box`: 2× RTX 4090, both idle, 46 GB free. It's ready to use.

The command is `fleet add "ssh ubuntu@10.0.0.7"`. In that one step fleet:

1. connects with whatever already works for you — your ssh-agent, your `~/.ssh/config`,
   a key file you named with `-i`;
2. puts the fleet's own key on the machine, so it no longer depends on yours;
3. gives the machine a fleet key of its own and records it: that key is the machine's
   identity, which access between machines is granted by;
4. measures it: CPU, memory, GPUs, disks, and what is free right now.

If the machine only accepts a password, the agent asks you to type it yourself, once. It
is used for one connection and stored nowhere. For every other case — a rental, a machine
fleet cannot reach, one that does not exist yet — see
[Adding machines](guides/add-machines.md).

## 5. Use it

Ask for what you need, not for a machine by name:

> **You:** what's free right now?

> **You:** run `nvidia-smi` on the box with the most free VRAM

The agent picks the machine from what each one has and what is free on it at that moment,
then runs the command there with `fleet ssh` and brings the result back. Nothing about how
to log in — hosts, keys, passwords — goes into the conversation.

To watch the whole fleet live yourself, run `fleet top` in a terminal (`q` quits).

## 6. Optional: a machine that runs fleet too

The machine you added can be *reached* by fleet; its own agents do not know about the
fleet yet. If you also work on that machine, ask on the center:

> **You:** invite gpu-box

You get one line. Paste it into a terminal on that machine, or give it to the agent there.
It installs fleet as a member and joins, with no password; from then on the agents on that
machine see the whole fleet too.

## Next steps

- [Adding machines](guides/add-machines.md) — rentals, password-only hosts, machines
  behind NAT, invites, cloud-init.
- [Finding the right machine](guides/find-machines.md) — what fleet measures, tags, costs
  and alerts.
- [Running work](guides/run-work.md) — commands, long jobs, files.
- [Access between machines](guides/access.md) — letting one machine reach another.
- [The center](guides/center.md) — what it does, when it is off, moving it.
- [All documentation](README.md)
