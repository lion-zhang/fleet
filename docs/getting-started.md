# Getting started

This walks you through fleet on the machine you work from: installing it, adding one
other machine, and a first job on that machine. It takes about ten minutes. You talk to
your coding agent the whole way. The command behind each step is shown too, for when you
would rather type it yourself.

You need:

- a coding agent that can run commands — Claude Code, Codex, Gemini CLI, Copilot CLI,
  OpenCode or [any other](guides/agents.md);
- one more machine you can already reach with `ssh` from this one: a server, a desktop,
  a cloud VM or a GPU rental. Nothing has to be installed on it. If the two are behind
  different NATs, put them on a mesh network such as Tailscale or ZeroTier first
  ([why](guides/add-machines.md#networks)).

fleet connects to that machine exactly as `ssh` does from here — your ssh config, your
keys, Tailscale SSH — so if `ssh` gets in, fleet does too. It manages no access of its
own, and never asks for a password.

## 1. Install fleet

Ask your agent:

```text
Install fleet from https://github.com/lion-zhang/fleet
```

The agent runs the installer. It installs the `fleet` command, records this machine, and
teaches every supported agent on this machine to use fleet — not only the one you asked.
It ends by listing what to try next. Start a new agent session afterwards, so the agent
loads fleet's skill.

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

## 2. Look at this machine

> **You:** show me this machine

The agent runs `fleet show` and describes what fleet measured: CPU and load, memory, GPUs
and their free VRAM, disks, the busiest processes, the services listening on ports, and
the *facts* agents use to pick a machine (`linux`, `cuda`, `vram-24g`, …).

## 3. Add a machine

Give the agent the SSH command you already use for the other machine:

> **You:** add `ssh ubuntu@10.0.0.7`
>
> **Agent:** Added as `gpu-box`: 2× RTX 4090, both idle, 46 GB free. It's ready to use.

The command is `fleet add "ssh ubuntu@10.0.0.7"`. fleet connects with whatever already
works for you — your ssh-agent, your `~/.ssh/config`, a key file you named with `-i` —
measures the machine, and records it in this machine's list. It writes nothing on the
other machine.

Or let the agent look for you:

> **You:** what machines could I add?

It runs `fleet ls --discover`, which lists the hosts in your `~/.ssh/config` and the
machines on your tailnet, and offers them to you; `fleet add NAME` adds one.

**If the machine does not let fleet in,** nothing is recorded, and fleet says why and
what to do — usually to put your key on it once. `fleet doctor NAME` prints the exact
line; at a terminal, `fleet add` offers to run `ssh-copy-id`, which asks for the machine's
password itself. For rentals, Windows and Mac machines and Tailscale SSH, see
[Reaching your machines](guides/reaching-machines.md).

## 4. Use it

Ask for what you need, not for a machine by name:

> **You:** what's free right now?

> **You:** run `nvidia-smi` on the box with the most free VRAM

The agent picks the machine from what each one has and what is free on it at that moment,
then runs the command there with `fleet ssh` and brings the result back. Passwords and
keys never go into the conversation; the agent may see a machine's address, as
`fleet show` prints it.

To watch every machine live yourself, run `fleet top` in a terminal (`q` quits).

## 5. Optional: fleet on another machine too

The machine you added is *watched* from here; its own agents do not use fleet. If you
also work on that machine, install fleet there the same way, or from here:

> **You:** install fleet on gpu-box

The agent runs `fleet install gpu-box`. Each machine running fleet keeps its own list, of
the machines its own ssh reaches — there is no center, and nothing to keep in sync. To
use the same list on both, share `machines.yaml` (`fleet paths` shows where) with your
dotfiles.

## Next steps

- [Reaching your machines](guides/reaching-machines.md) — Tailscale, Windows and Mac,
  rentals, and what to do when a machine refuses.
- [Adding machines](guides/add-machines.md) — names, tags, rentals, clones, removing.
- [Finding the right machine](guides/find-machines.md) — what fleet measures, tags, costs
  and alerts.
- [What is on each machine](guides/resources.md) — GPU jobs, served models, software,
  where models and data live.
- [Running work](guides/run-work.md) — commands, long jobs, files.
- [All documentation](README.md)
