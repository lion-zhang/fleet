<!-- mcp-name: io.github.lion-zhang/fleet -->
<div align="center">

# fleet — let your coding agent use every machine you have

**Claude Code, Codex and Gemini see one machine: the one they run on.<br>
fleet shows them all of yours — every GPU, how much is free, and how to get there.**

[![tests](https://github.com/lion-zhang/fleet/actions/workflows/test.yml/badge.svg)](https://github.com/lion-zhang/fleet/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Linux | macOS | Windows](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey.svg)
![MCP server](https://img.shields.io/badge/MCP-server-8A2BE2.svg)

<img src="docs/assets/fleet-ls.svg" alt="fleet ls: free GPU, VRAM, CPU, RAM and disk across seven machines — an A100 rental with one busy and one idle card, an idle H100 rental flagged as costing money, a busy RTX 3090 box with a nearly full disk, a laptop, a NAS and a machine that is switched off" width="100%">

</div>

## The problem

Ask your agent to train a model and it starts on your laptop — while a 4090 sits idle
across the room and a rented A100 bills you by the hour. It cannot use what it cannot see.

- **Your agent is stuck on one machine.** It has no idea your other boxes exist.
- **Finding a free GPU is manual.** SSH into five hosts, run `nvidia-smi`, compare in your head.
- **Handing it a server means pasting credentials** into the chat, and hoping.

## Get started: ask your agent

Install it on the machine that will be your fleet's **center** — the one that keeps the
list of your machines and decides which may reach which — so pick one that can reach
all your devices and is online most of the time, such as a desktop or home server (you
can move the role later). There, paste this into Claude Code, Codex, Gemini CLI, or any
agent that can run commands:

```text
Install fleet from https://github.com/lion-zhang/fleet
```

<sub>Agents: the steps are in [INSTALL.md](INSTALL.md).</sub>

That is the whole setup. fleet is installed once on this machine, and every
[supported agent](docs/guides/agents.md) you have here learns it — not only the one you asked.
From here on you say what you want in plain words, with no commands to remember.

### Then add your machines

Tell the agent how you reach a machine — the SSH command you already use is enough:

> **You:** add my GPU server, `ssh ubuntu@10.0.0.7`
>
> **Agent:** Added as `gpu-box`: 2× RTX 4090, both idle, 46 GB free. It's ready to use.

Each one takes a sentence, whatever the machine is:

| The machine | What you tell the agent |
|---|---|
| a server you can already SSH into | "add `ssh ubuntu@10.0.0.7`" — fleet uses the key you already have |
| one you reach with a particular key file | "add `ssh -i ~/.ssh/my_key ubuntu@1.2.3.4`" — or a `Host` from your `~/.ssh/config` |
| a new rental (vast.ai, RunPod, AutoDL) | "add `ssh -p 40001 root@1.2.3.4`, it costs $1.89/hr" |
| one that only takes a password | "add it" — then you type the password once yourself; it is stored nowhere |
| one fleet cannot get into from here | "invite my laptop" — you get one line to paste there, and it joins by itself |
| one that does not exist yet | "give me the key for a cloud-init template" — it joins with no password at all |

Nothing is installed on the machines you add: they only need SSH, on Linux, macOS or
Windows. fleet probes each one for its GPUs, memory and disks, and keeps that current.

### Then let the agent pick

You describe the work; the agent finds where it fits. It checks what each machine has
*and* what is free on it right now, so it will not send a job to a busy card:

> **You:** train `train.py` on whatever has a free 24 GB card
>
> **Agent:** `rtx4090` has 23.1 GB free and an idle GPU; `a100-spot` is free too but costs
> $1.89/hr. Starting on `rtx4090`, logging to `train.log`.

| You say | How the agent finds it |
|---|---|
| "what's free right now?" | every machine checked, the free ones listed |
| "find me a box with a 24 GB card" | matched by what machines have — NVIDIA, VRAM, cores, RAM — not by name |
| "run the tests on the Linux box" | the machine that runs Linux, results brought back |
| "what's costing me money?" | idle paid rentals flagged, with their hourly price |
| "let the laptop reach the NAS" | access granted, applied at once |

Keys and passwords never go into the conversation, and anything irreversible waits for
your yes.

**Only install fleet where you use it.** The machines you add above need nothing but SSH:
fleet measures them and connects to them from the center. Install fleet on another
machine only if you also run agents there, or want to check the fleet from it. That
machine becomes a **member**: ask the center's agent to "invite" it, and paste the line it
gives you into a terminal there, or give it to the agent there.

| | Runs fleet | How it gets there |
|---|---|---|
| **center** | yes — one per fleet | the first install |
| **member** | yes | an invite line from the center |
| every other machine | no, only SSH | "add `ssh user@host`" on the center |

### An app that cannot run commands?

Desktop apps and editors get fleet in one click; it runs as an MCP server:

| App | Install |
|---|---|
| **Claude Desktop** | open `fleet.mcpb` from the [latest release](https://github.com/lion-zhang/fleet/releases/latest) |
| **Cursor** | [![Add to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=fleet&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJhZ2VudHMtZmxlZXQiLCJtY3AiXX0%3D) |
| **VS Code** | [![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_fleet-0098FF?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agents-fleet%22%2C%22mcp%22%5D%7D) |

Plus plugins for Claude Code, Codex, Copilot CLI and Gemini CLI, and OpenCode, Amp,
Windsurf, Cline, Zed, Qwen Code, Goose, Kiro, Hermes — 35+ agents in all:
**[every agent →](docs/guides/agents.md)**

## Built to be safe

- **Nothing to install on your machines.** fleet probes with one script over one SSH
  connection — Linux, macOS or Windows. A NAS or a fresh rental works as it is.
- **Keys, not passwords.** A password, if needed at all, is typed once by you. Nothing
  that could be stolen is stored, and the agent never sees a credential.
- **Access you control.** The center decides which machine may reach which, and a revoke
  is pushed at once. `fleet access gpu-box --allow laptop`, `--deny` to take it back.
- **The agent asks, not guesses.** It is told to ask which machine you mean, and to leave
  irreversible commands to you.

## Prefer the command line?

Everything the agent does is a plain `fleet` command. To install fleet yourself, run this
on the machine you work from (this is also what your agent runs when you ask it):

```bash
curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh
```

<sub>Windows: `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"`</sub>

Then drive it yourself:

```bash
fleet ls                            # every machine, with what is free right now
fleet ls --tag cuda --tag vram-24g  # by capability: NVIDIA, a card of 24 GB or more
fleet top                           # live view, like htop for the whole fleet
fleet show gpu-box                  # one machine in detail: GPU processes, services, disks
fleet ssh gpu-box -- nvidia-smi     # run something there
fleet add "ssh ubuntu@10.0.0.7"     # add a machine; fleet invite NAME for a join line
fleet access nas --allow laptop     # let one machine reach another
```

<img src="docs/assets/fleet-top.svg" alt="fleet top: a live view of GPU utilisation, free VRAM, CPU, RAM and disk across all machines" width="100%">

Rentals from vast.ai, RunPod or AutoDL show their price (`fleet edit a100 --cost 1.89`),
the fleet's burn rate, and an alert when a rental sits idle. On Tailscale, ZeroTier
or WireGuard? fleet just needs an address it can route to.

## How fleet compares

fleet is about the machines you already have. It complements tools that launch new ones.

| | `ssh` + `nvidia-smi` | nvitop / gpustat | SkyPilot / dstack | **fleet** |
|---|:---:|:---:|:---:|:---:|
| All your machines in one view | ✗ | ✗ one machine | ✓ the ones it manages | ✓ |
| Built for coding agents (skill / MCP) | ✗ | ✗ | partly | ✓ |
| Nothing installed on target machines | ✓ | ✗ | ✗ | ✓ |
| Manages SSH access between machines | ✗ | ✗ | for its own clusters | ✓ |
| Launches new cloud VMs | ✗ | ✗ | ✓ | ✗ |

## FAQ

**Setting up**

<details>
<summary><b>Do I need to install fleet on every machine?</b></summary>

No. Install it where you **use** it: the machine you work from (the center), and any
other machine where you also run agents or want to check the fleet from (a member). The
machines you only *use* — GPU servers, rentals, a NAS — need SSH and nothing else; fleet
measures them and connects to them from the center.
</details>

<details>
<summary><b>What are the center and members?</b></summary>

The **center** is the machine you installed fleet on first. It keeps the list of machines,
holds the key that reaches them, and is the only machine that grants or removes access.
There is one per fleet. A **member** is any other machine that runs fleet, joined with an
invite line from the center; its agents see the whole fleet too. Every other machine is
just *in* the fleet: reachable over SSH, nothing installed. See
[The center](docs/guides/center.md).
</details>

<details>
<summary><b>Which machine should be the center?</b></summary>

One that can reach all your devices and is online most of the time — a desktop or a home
server. A laptop works too. You can hand the role to another machine later with
`fleet center NAME`.
</details>

<details>
<summary><b>What if the center is off?</b></summary>

Normal — it can be a laptop that is closed half the day. Everything already granted keeps
working, and members keep listing and connecting from what they know; only *changes* —
granting, removing, inviting — wait for it.
</details>

<details>
<summary><b>I use Claude Code <i>and</i> Codex (and more) on one machine. Does that work?</b></summary>

Yes — that is the normal case. fleet is installed once per machine: one command, one
inventory, one set of keys. Each agent only gets a small skill or MCP entry pointing at
it, so Claude Code, Codex, Gemini CLI and a desktop app all see the same machines, and can
use them at the same time. Install a new agent later? Ask an agent that has fleet to run
`fleet setup`.
</details>

<details>
<summary><b>What does my agent actually get?</b></summary>

Agents that can run commands (Claude Code, Codex, Gemini CLI, Copilot CLI, OpenCode, …)
get a skill — text that tells them the `fleet` commands; it costs nothing until a task
needs a machine. Apps that cannot (Claude Desktop, Cursor, VS Code, …) get an MCP server
that runs the same commands for them. See [Agents](docs/guides/agents.md). After
installing, start a new session so the agent loads it.
</details>

**Safety**

<details>
<summary><b>Does fleet store my passwords? Does my agent see my keys?</b></summary>

No and no. A password is typed by you, once, for a machine that accepts nothing else; it
is used for one connection and stored nowhere. After that fleet uses its own key. The
agent runs `fleet ssh NAME` and never sees a key or a password; it may see a machine's
address, as `fleet show` prints it.
</details>

<details>
<summary><b>What does fleet leave on the machines I add?</b></summary>

Two things: a block in `~/.ssh/authorized_keys`, marked with your fleet's id, and the
machine's own fleet key in fleet's folder (`~/.config/fleet` on Linux). No program, no
service. Your own keys and
the provider's are never touched, and `fleet rm` takes fleet's block off again.
</details>

<details>
<summary><b>Can the agent do something I cannot undo?</b></summary>

It is told not to. Removing a machine, taking the fleet down and moving the center are
left to you, and are not available to apps over MCP at all. When a request is ambiguous —
which machine, which user — the agent asks instead of guessing.
</details>

<details>
<summary><b>What if a machine is compromised?</b></summary>

Remove it on the center (`fleet rm NAME`): its key comes off every other machine. But a
key is a key — revoking it does not undo what someone with root on that machine already
did — and the center is the one machine whose compromise reaches everything. The
[threat model](docs/design/access.md#threat-model-plainly) spells it out.
</details>

**Machines**

<details>
<summary><b>The disk space fleet reports is wrong.</b></summary>

Some systems — containers, GPU rentals — show `/` as a small overlay while the space is on
another volume. Tell fleet which paths to watch: "on gpu-box, watch /workspace", or
`fleet edit gpu-box --disk-path /workspace`. See
[Disks](docs/guides/find-machines.md#disks-tell-fleet-where-the-space-is).
</details>

<details>
<summary><b>My rental came back on a new IP or port.</b></summary>

Re-point it, keeping its name, tags, cost and history: "gpu-box is now at
`ssh -p 40123 root@5.6.7.8`", or `fleet edit gpu-box --ssh "ssh -p 40123 root@5.6.7.8"`.
</details>

<details>
<summary><b>My machines are behind different NATs, or at different sites.</b></summary>

fleet does not solve connectivity; it uses the routes you have. It needs only one: **the
center must reach every machine over SSH** (and members must reach the center). The
machines do not need to reach each other, unless you grant one access to another. For
machines behind different NATs, put them on a mesh network such as Tailscale or ZeroTier
and add them by their mesh address. See
[Networks](docs/guides/add-machines.md#networks-what-must-reach-what).
</details>

<details>
<summary><b>Windows?</b></summary>

Yes, both ways. A Windows machine works in a fleet with OpenSSH Server and nothing else,
and fleet runs on Windows too, center included. See [Windows](docs/guides/windows.md).
</details>

<details>
<summary><b>Does anything keep running in the background?</b></summary>

On the center only: a small service, run as you, that listens on port 7373 so members can
refresh and new machines can join. Nothing runs on the other machines; members run fleet
only when you or an agent use it.
</details>

<details>
<summary><b>How do I remove fleet?</b></summary>

On a member: `fleet center --leave`, `fleet setup --uninstall`, then
`uv tool uninstall agents-fleet`. On the center, `fleet center --dissolve` first takes
every key off every machine. See [Removing fleet](docs/guides/install.md#removing-fleet).
</details>

## Learn more

- [Getting started](docs/getting-started.md) — your first fleet, in ten minutes
- [Guides](docs/README.md#guides) — adding machines, finding the right one, access, the center, Windows
- [Every agent](docs/guides/agents.md) — install commands and config for 35+ agents
- [Command reference](docs/reference/cli.md) — every command and option
- [How fleet works](docs/design/how-it-works.md) — what happens, step by step, under the hood
- [Design](docs/design/layers.md) — one core per machine, a skill per agent, MCP for the rest; [access](docs/design/access.md) and [sync](docs/design/sync.md)
- [All documentation](docs/README.md)

**Status:** v0.5. The test suite runs on Linux, macOS and Windows, and every command is
run end to end on a real machine of each OS in CI — from a script, as an agent runs it,
and at a real terminal, as you do. Multi-machine fleets (key, password, invite, handover)
are tested on Linux machines built from scratch.

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). If fleet saved
you a GPU-hour, a ⭐ helps other people find it.

[MIT](LICENSE)
