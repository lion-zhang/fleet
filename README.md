<!-- mcp-name: io.github.lion-zhang/fleet -->
<div align="center">

# fleet — GPU & machine inventory for coding agents

**Give your coding agent every machine you have.**

A live view of all your GPUs, servers and rentals — for you and for **Claude Code, Codex
and Gemini CLI** — so the experiment lands on the machine with 80 GB free, not the one
already at 100%.

[![tests](https://github.com/lion-zhang/fleet/actions/workflows/test.yml/badge.svg)](https://github.com/lion-zhang/fleet/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![Linux | macOS | Windows](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey.svg)
![MCP server](https://img.shields.io/badge/MCP-server-8A2BE2.svg)

[Why](#why-fleet) · [Quick start](#quick-start) · [Ask your agent](#let-your-coding-agent-run-experiments-on-your-servers) ·
[Agents](#works-with-your-agent) · [How it works](#how-it-works) · [Compare](#how-fleet-compares) · [FAQ](#faq)

<img src="docs/assets/fleet-ls.svg" alt="fleet ls: free GPU, VRAM, CPU, RAM and disk across seven machines — an A100 rental with one busy and one idle card, an idle H100 rental flagged as costing money, a busy RTX 3090 box with a nearly full disk, a laptop, a NAS and a machine that is switched off" width="100%">

</div>

## Why fleet

Coding agents only know the machine they are running on. Ask Claude Code to train a model
and it will happily start on your laptop, while a 4090 sits idle across the room and a
rented A100 bills you by the hour.

| ❌ Without fleet | ✅ With fleet |
|---|---|
| Your agent sees one machine: the one it runs on. | It sees every machine you have, and what each has free right now. |
| "Which box is free?" means sshing into five hosts and running `nvidia-smi`. | One table, live: `fleet ls`, or `fleet ls --tag vram-24g` for "a card with 24 GB or more". |
| Letting an agent use your servers means pasting hosts and passwords into the chat. | `fleet ssh gpu-box -- python train.py`. No credential ever reaches the transcript. |
| The rented H100 sat idle all weekend. | Idle rentals are flagged with what they cost per hour. |
| Rentals recycle IPs and ports, and your `~/.ssh/config` rots. | `fleet edit box --ssh "ssh -p 40001 root@…"` keeps its name, tags and history. |
| Machine A reaching machine B means copying keys by hand, and nobody ever revokes them. | One center grants and revokes, revokes are pushed at once, and `fleet invite` adds a machine with no password. |

And **nothing to install** on the machines you connect to: the probe is one script over
one SSH connection, POSIX `sh` or PowerShell, whichever answers.

## Quick start

A fleet has two kinds of machine. The **center** is the one that decides who may reach
what; install it on the machine you work from. Every other machine is a **member**.

| Install as | Where | Command |
|---|---|---|
| **center** | the machine you work from — once per fleet | `curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh \| sh` |
| **member** | each other machine you want to run fleet on | the line `fleet invite` prints on the center: the same installer, ending `sh -s -- --join fleet1:…` |

On Windows, the center line is `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"`,
and `fleet invite` prints the member line for PowerShell too.

The center install also teaches every coding agent it finds there to use fleet. Then add
machines from the center:

```bash
fleet invite gpu-box                # prints the member line to paste on gpu-box
fleet add "ssh root@gpu-box"        # or have the center reach it: probe, key, record
```

A member joins with no password, even from behind NAT. Where fleet is installed already,
`fleet join fleet1:…` does the same. Machines you only connect *to* — added with `fleet
add` — are members too, and need nothing installed.

Then just ask your agent.

### Or install from inside your agent

| Agent | Install | What it gets |
|---|---|---|
| **Claude Code** | `/plugin marketplace add lion-zhang/fleet` then `/plugin install fleet@fleet` | the fleet skill, and a start-up note on how to install the CLI if it is missing |
| Claude Code, as MCP | `claude mcp add --scope user fleet -- uvx agent-fleet mcp` | fleet's tools over MCP |
| **Codex CLI** | `codex mcp add fleet -- uvx agent-fleet mcp` | fleet's tools over MCP |
| **Gemini CLI** | `gemini extensions install https://github.com/lion-zhang/fleet` | MCP tools and the skill |
| **Claude Desktop** | download `fleet.mcpb` from the [latest release](https://github.com/lion-zhang/fleet/releases/latest) and open it | MCP tools |
| **Cursor** | [![Add to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=fleet&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJhZ2VudC1mbGVldCIsIm1jcCJdfQ==) | MCP tools |
| **VS Code** (Copilot agent mode) | [![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_fleet-0098FF?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agent-fleet%22%2C%22mcp%22%5D%7D) or `code --add-mcp '{"name":"fleet","command":"uvx","args":["agent-fleet","mcp"]}'` | MCP tools |
| **Hermes**, **Windsurf** | install the CLI (above), then `fleet setup` | the skill / MCP tools |
| OpenCode, Cline, Amp, Goose, … | `npx skills add lion-zhang/fleet` | the skill |
| Any other MCP client | `{"command": "uvx", "args": ["agent-fleet", "mcp"]}` | MCP tools |

Every row runs the same `fleet` from PyPI ([agent-fleet](https://pypi.org/project/agent-fleet/)),
so the agents share one fleet. The skills need the CLI itself; if it is missing they say
so and give the line above. MCP rows need only [uv](https://docs.astral.sh/uv/).

As a plain Python tool: `uv tool install agent-fleet` or `pipx install agent-fleet`, then
`fleet setup` to teach your agents. Want to look before installing anything?
`uvx agent-fleet show` describes the machine you are on — CPU, RAM, GPUs, disks,
services.

However fleet arrives, the first command on a machine that is in no fleet makes it the
center, once, and says so; an empty fleet started that way steps aside for `fleet join`.
Set `FLEET_NO_AUTO_CENTER=1` to stop it. The full walkthrough is in
[docs/getting-started.md](docs/getting-started.md).

## Let your coding agent run experiments on your servers

After `fleet setup`, the agent knows your machines the way it knows `git`. You ask in
words; it picks the command. For example (an illustrative session):

> **You:** train `train.py` on whatever has a free 24 GB card
>
> **Agent:** runs `fleet ls --tag cuda --tag vram-24g --json` — `rtx4090` has 23.1 GB free
> and an idle GPU; `a100-spot` has one idle 80 GB card but costs $1.89/hr; `lab-3090` is
> busy. Starting on `rtx4090`:
> `fleet ssh rtx4090 -- 'cd ~/proj && nohup python train.py > train.log 2>&1 &'`

Agents are told to **ask rather than guess**, so a vague request comes back as a question,
and the irreversible commands (`fleet rm`, handing over the center) stay with you.
Things people ask:

| You say | The agent runs |
|---|---|
| "what's free right now?" | `fleet ls --json` |
| "find me a box with a 24G card" | `fleet ls --tag cuda --tag vram-24g --json` |
| "what's costing me money?" | `fleet ls --json`, and reads the alerts |
| "add my new rental, `ssh -p 40001 root@1.2.3.4`" | `fleet add "ssh -p 40001 root@1.2.3.4"` |
| "let the laptop reach the NAS" | `fleet access nas --allow laptop` |

## Find a free GPU across all your machines

```bash
fleet ls                            # every machine, with what is free right now
fleet ls --tag cuda --tag vram-24g  # by capability, not by remembering names
fleet top                           # live view, like htop for the fleet
fleet show gpu-box                  # one machine in detail: GPU processes, services, disks
```

<img src="docs/assets/fleet-top.svg" alt="fleet top: a live view of GPU utilisation, free VRAM, CPU, RAM and disk across all machines" width="100%">

**Facts are measured, tags are yours.** Every probe derives facts such as `gpu`, `cuda`,
`metal`, `vram-24g`, `cores-32`, `ram-64g`, `linux`, `arm64`, `rental`, `public-ip`. Size
facts mean "at least", so `--tag vram-24g` finds an 80 GB card too. Your own labels
(`fleet edit box --tag nas`) sit beside them, and one `--tag` searches both.

## Highlights

- **Built for agents.** A skill for agents with a shell; an MCP server for desktop clients.
  Updating fleet refreshes what every agent is told, on every machine.
- **Every machine at once.** GPUs, free VRAM, GPU processes, CPU, RAM, disks and listening
  services, probed in parallel. A machine that is off costs a timeout once, then backs off.
- **Nothing on the targets.** One script over one SSH connection, on Linux, macOS and
  Windows (PowerShell). A NAS or a fresh rental works as it is.
- **Keys, not passwords.** A password, if one is needed at all, is typed once and spent
  on a single connection. Nothing that could be stolen is stored.
- **Access you can revoke.** One center decides who may reach what. Revokes are pushed at
  once, `fleet rm` takes keys off both ways, and joining needs a single-use invite.
- **Costs in view.** `$/hr` per rental, the total burn rate, and an alert when a paid
  machine is doing nothing.
- **Survives the center being off.** The center can be a laptop that is closed half the
  day: everything already granted keeps working; only changes wait.

## Works with your agent

| Agent | How it learns fleet |
|---|---|
| Claude Code | skill: `~/.claude/skills/fleet/SKILL.md` |
| Codex | skill: `~/.codex/skills/fleet/SKILL.md` |
| Gemini CLI | a marked region in `~/.gemini/GEMINI.md` |
| Hermes | skill, in Hermes' own home (`%LOCALAPPDATA%\hermes` on Windows) |
| Claude Desktop, Cursor, VS Code, Windsurf | MCP server: `fleet mcp` |

`fleet setup` sets up whichever of these are installed and nothing else. It never
creates a config directory for an agent you do not use, and in files you own it edits
only its own marked region.

<details>
<summary><b>Use it as an MCP server</b> (Claude Desktop, Cursor, VS Code, Windsurf)</summary>

Every install includes the MCP server. With the CLI installed, `fleet setup` registers
it with every desktop client it finds. Without it, use the rows in the install table
above: they launch `uvx agent-fleet mcp`.

Or add it to a client's config by hand — `mcpServers` for most clients, `servers` for
VS Code. Use the full path that `which fleet` prints: a desktop app on macOS does not
see `~/.local/bin` on its PATH.

```json
{
  "mcpServers": {
    "fleet": { "command": "/Users/you/.local/bin/fleet", "args": ["mcp"] }
  }
}
```

</details>

<details>
<summary><b>Project scope</b> (one repo, not your whole home)</summary>

`fleet setup --project` writes into the current directory: `.claude/skills/fleet/SKILL.md`
by default, or with `--target codex` (or `hermes`, `gemini`) a marked region in the
`AGENTS.md` / `GEMINI.md` those agents read.

</details>

## How it works

- **One center decides.** The machine you installed fleet on first is the only one that
  installs or removes keys. You can hand the role to another machine with `fleet center
  NAME`; every machine follows by itself.
- **Enforced by sshd, not by fleet.** Grants are keys in `authorized_keys`, inside
  labelled blocks that fleet alone edits. Your own keys and other tools' keys are never
  touched.
- **Signed sync.** Machines refresh from the center over a small listener; everything that
  crosses it is signed by a key the fleet already pinned.
- **Probes are one script.** No agent, no daemon, no Python on the far side.

The design, and what it protects against, is in [docs/design/access.md](docs/design/access.md).

## Manage SSH access between machines

```bash
fleet access gpu-box --allow laptop      # laptop may now reach gpu-box, applied at once
fleet access gpu-box --deny laptop       # and no longer may, pushed at once
fleet access                             # who may reach what, and anything pending
fleet center --leave                     # take this machine out, no permission needed
```

## How fleet compares

fleet is about the machines you already have. It complements tools that launch new ones.

| | `ssh` + `nvidia-smi` | nvitop / gpustat | SkyPilot / dstack | **fleet** |
|---|:---:|:---:|:---:|:---:|
| All your machines in one view | ✗ | ✗ one machine | ✓ the ones it manages | ✓ |
| Built for coding agents (skill / MCP) | ✗ | ✗ | partly (dstack ships agent skills) | ✓ |
| Nothing installed on target machines | ✓ | ✗ | ✗ | ✓ |
| Manages SSH access between machines | ✗ | ✗ | for its own clusters | ✓ |
| Idle paid machines | ✗ | ✗ | ✓ SkyPilot autostops them | flags them |
| Rich per-process GPU view | ✗ | ✓ nvitop is excellent | ✗ | basic, in `fleet show` |
| Launches new cloud VMs | ✗ | ✗ | ✓ | ✗ |

Running GPU rentals on vast.ai, RunPod or Lambda? Add them like any machine with
`--kind rental`, give each its price with `fleet edit a100 --cost 1.89`, and fleet shows
the burn rate and flags the ones that are idle. On Tailscale,
ZeroTier or WireGuard? fleet just needs an address it can route to.

## FAQ

<details>
<summary><b>How do I let Claude Code use my GPU server?</b></summary>

`fleet add "ssh you@server"` on the center, then `fleet setup`. Claude Code reads the
fleet skill and runs `fleet ls` / `fleet ssh` itself. No hostnames or keys go into the
conversation.
</details>

<details>
<summary><b>Is it safe to hand my agent all my machines?</b></summary>

The agent gets exactly what you could do with `fleet ssh`, and no credential appears in
the transcript. Agents are told never to type or accept a password, to ask rather than
guess which machine is meant, and to leave the irreversible commands to you.
</details>

<details>
<summary><b>Do the target machines need anything installed?</b></summary>

No — only sshd. Install fleet itself only on machines you want to run fleet commands from.
</details>

<details>
<summary><b>Does it work behind NAT, or across sites?</b></summary>

fleet needs an address the center can route to: a public IP, a LAN, or an overlay such as
Tailscale or ZeroTier. A machine the center cannot dial can still join with `fleet join`
and report in. Granting access to it waits until the center can reach it.
</details>

<details>
<summary><b>Windows?</b></summary>

Windows machines work as targets with nothing to configure, and fleet runs on Windows,
center included. The one gap: a Windows center cannot type a password, because there is
no pty, so put `fleet center --pubkey` on the host first or use `fleet invite`.
</details>

<details>
<summary><b>What if the center is off, or lost?</b></summary>

Off is normal: everything already granted keeps working, and changes wait for it.
Lost is not: there is no automatic failover, by design. Hand over before retiring the
center, and keep a copy of `fleet center --export`.
</details>

## Status

v0.4. Every command has been run end to end on a fleet of Linux machines built from
scratch, and the test suite runs on Linux and macOS. Windows works as a target and as a
center, and is less tested. Next: a PyPI release, an approval-by-code join, and grants a machine applies to
itself so that machines the center cannot reach can be fully managed.

## Contributing

Issues and pull requests are welcome. Run the tests with `uv run pytest -q`. The README
screenshots are generated, never edited: `uv run python scripts/readme_screenshots.py`.

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=lion-zhang/fleet&type=Date)](https://star-history.com/#lion-zhang/fleet&Date)

If fleet saved you a GPU-hour, a ⭐ helps other people find it.

## License

[MIT](LICENSE)
