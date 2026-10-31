# Agents

**Most people never need this page.** Asking an agent to install fleet — or running the
installer — sets up every supported agent on the machine at once, with `fleet setup`.
This page is for adding fleet from inside one particular agent or app, or to an agent
`fleet setup` does not know.

Every way in runs the same `fleet` from PyPI ([agents-fleet](https://pypi.org/project/agents-fleet/)),
so all your agents on a machine share one fleet: one list of machines, one cache
([why](../design/layers.md)). Two kinds of entry:

- **Skills** are for agents that can run commands. A skill is text that tells the agent
  how to use the `fleet` command, and costs nothing until a task needs a machine. It needs
  the `fleet` command installed; if it is missing, the skill says how to get it.
- **MCP** is for apps that cannot run commands. The app launches `uvx agents-fleet mcp`,
  which runs the same commands for it. It needs only [uv](https://docs.astral.sh/uv/).

> **However it is installed, it is the same fleet.** There are no modes to choose. The
> first use of fleet on a machine — through a plugin, an MCP app or a shell — records the
> machine and teaches the other agents found there, once (`FLEET_NO_FIRST_RUN=1` skips
> that). fleet then reaches other machines with your own ssh, exactly as you do.

**✓** = run here, and the agent registered (or connected to) the server.
**docs** = from the agent's official documentation, not run here.

- [Plugins and extensions](#plugins-and-extensions) — the skill, installed by the agent
- [One command](#one-command) — `mcp add` and similar
- [One click](#one-click) — install links and bundles
- [Config files](#config-files) — everything else
- [Skills only](#skills-only) and [what `fleet setup` does](#what-fleet-setup-does)

## Plugins and extensions

The agent installs fleet's skill, which teaches it the `fleet` commands and costs nothing
until a task needs a machine.

| Agent | Install | |
|---|---|---|
| Claude Code | `/plugin marketplace add sinolion/fleet` then `/plugin install fleet@fleet`<br>(shell: `claude plugin marketplace add sinolion/fleet && claude plugin install fleet@fleet`) | ✓ |
| Codex | `codex plugin marketplace add sinolion/fleet && codex plugin add fleet@fleet` | ✓ |
| GitHub Copilot CLI | `copilot plugin marketplace add sinolion/fleet && copilot plugin install fleet@fleet` | ✓ |
| Gemini CLI | `gemini extensions install https://github.com/sinolion/fleet` — the skill and the MCP server | ✓ |

## One command

These register fleet's MCP server, launched as `uvx agents-fleet mcp`.

| Agent | Command | |
|---|---|---|
| Claude Code | `claude mcp add --scope user fleet -- uvx agents-fleet mcp` | ✓ |
| Codex (CLI, IDE, ChatGPT app) | `codex mcp add fleet -- uvx agents-fleet mcp` | ✓ |
| GitHub Copilot CLI | `copilot mcp add fleet -- uvx agents-fleet mcp` | ✓ |
| Amp | `amp mcp add fleet -- uvx agents-fleet mcp` | ✓ |
| OpenCode | `opencode mcp add fleet -- uvx agents-fleet mcp` | ✓ |
| Qwen Code | `qwen mcp add fleet uvx agents-fleet mcp` | ✓ |
| Auggie (Augment CLI) | `auggie mcp add fleet -- uvx agents-fleet mcp` | ✓ |
| Factory Droid | `droid mcp add fleet "uvx agents-fleet mcp"` | ✓ |
| Kiro CLI | `kiro-cli mcp add --name fleet --scope global --command uvx --args agents-fleet --args mcp` | ✓ |
| Mistral Vibe | `vibe mcp add fleet --transport stdio --command uvx --arg agents-fleet --arg mcp` | ✓ |
| VS Code (Copilot agent mode) | `code --add-mcp '{"name":"fleet","command":"uvx","args":["agents-fleet","mcp"]}'` | docs |
| Windsurf / Devin Desktop | `devin mcp add fleet -s user -- uvx agents-fleet mcp` | docs |
| Jan | `jan cli mcp add fleet --command uvx --arg agents-fleet --arg mcp --active` | docs |
| Crush | `echo 'mcp add fleet --command uvx --args agents-fleet --args mcp' >> ~/.config/crush/crushrc` | docs |

## One click

| Agent | Link |
|---|---|
| Cursor | [Add to Cursor](https://cursor.com/en/install-mcp?name=fleet&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJhZ2VudHMtZmxlZXQiLCJtY3AiXX0%3D) |
| VS Code | [Install in VS Code](https://insiders.vscode.dev/redirect/mcp/install?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agents-fleet%22%2C%22mcp%22%5D%7D) · [VS Code Insiders](https://insiders.vscode.dev/redirect/mcp/install?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agents-fleet%22%2C%22mcp%22%5D%7D&quality=insiders) |
| Visual Studio | [Install in Visual Studio](https://vs-open.link/mcp-install?%7B%22name%22%3A%22fleet%22%2C%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agents-fleet%22%2C%22mcp%22%5D%7D) |
| Kiro | [Add to Kiro](https://kiro.dev/launch/mcp/add?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agents-fleet%22%2C%22mcp%22%5D%7D) |
| LM Studio | [Add to LM Studio](https://lmstudio.ai/install-mcp?name=fleet&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJhZ2VudHMtZmxlZXQiLCJtY3AiXX0%3D) |
| Goose | `goose://extension?cmd=uvx&arg=agents-fleet&arg=mcp&id=fleet&name=fleet&description=fleet&timeout=300` (paste into a browser) |
| Claude Desktop | download `fleet.mcpb` from the [latest release](https://github.com/sinolion/fleet/releases/latest) and open it ✓ |

These links are built from each agent's documented format; the Claude Desktop bundle was
validated, packed and run here.

## Config files

For everything else, add fleet to the agent's config. Most agents read the same shape:

```json
{
  "mcpServers": {
    "fleet": { "command": "uvx", "args": ["agents-fleet", "mcp"] }
  }
}
```

| Agent | File (user scope) | Notes |
|---|---|---|
| Cursor (IDE and CLI) | `~/.cursor/mcp.json` | |
| Windsurf / Devin Desktop | `~/.config/devin/mcp_config.json` (Windows `%APPDATA%\devin\`); the older `~/.codeium/windsurf/mcp_config.json` is still read | |
| Claude Desktop | macOS `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows `%APPDATA%\Claude\claude_desktop_config.json` | |
| Cline (VS Code) | MCP Servers → Configure → *Configure MCP Servers* (`cline_mcp_settings.json`) | add `"disabled": false` |
| Cline CLI | `~/.cline/data/settings/cline_mcp_settings.json` | add `"disabled": false` |
| Kiro (IDE and CLI) | `~/.kiro/settings/mcp.json` | |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | |
| Qwen Code | `~/.qwen/settings.json` | |
| Factory Droid | `~/.factory/mcp.json` | |
| Auggie | `~/.augment/settings.json` | |
| Junie (IDE and CLI) | `~/.junie/mcp/mcp.json` | |
| JetBrains AI Assistant | Settings → Tools → AI Assistant → Model Context Protocol → **+** → *As JSON* | paste the block above |
| Augment Code (IDE) | Augment settings → MCP → *Import from JSON* | paste the block above |
| Kimi Code | `~/.kimi-code/mcp.json` | |
| Warp | `~/.warp/.mcp.json` | |
| LM Studio | Program → Install → *Edit mcp.json* | |
| Trae | Settings → MCP → Add → *Add Manually* | paste the block above |
| Cherry Studio | Settings → MCP Servers → Add → import from JSON | |
| BoltAI | `~/.boltai/mcp.json` | |
| Jan | Settings → MCP Servers → **+** | add `"active": true` |
| Msty Studio | Toolbox → Tools → Add New Tool → *STDIO / JSON* | the inner object only: `{"command": "uvx", "args": ["agents-fleet", "mcp"]}` |

The agents below use their own shapes.

<details>
<summary><b>VS Code</b> — user <code>mcp.json</code> (<i>MCP: Open User Configuration</i>), or <code>.vscode/mcp.json</code> in a project</summary>

```json
{ "servers": { "fleet": { "type": "stdio", "command": "uvx", "args": ["agents-fleet", "mcp"] } } }
```

Visual Studio reads the same shape from `%USERPROFILE%\.mcp.json`.
</details>

<details>
<summary><b>OpenCode</b> (<code>~/.config/opencode/opencode.json</code>) and <b>Kilo Code</b> (<code>~/.config/kilo/kilo.json</code>) — ✓</summary>

```json
{ "mcp": { "fleet": { "type": "local", "command": ["uvx", "agents-fleet", "mcp"], "enabled": true } } }
```
</details>

<details>
<summary><b>Zed</b> — <code>~/.config/zed/settings.json</code> (Windows <code>%APPDATA%\Zed\settings.json</code>)</summary>

```json
{ "context_servers": { "fleet": { "command": "uvx", "args": ["agents-fleet", "mcp"], "env": {} } } }
```
</details>

<details>
<summary><b>Continue</b> — <code>~/.continue/config.yaml</code> (agent mode only)</summary>

```yaml
mcpServers:
  - name: fleet
    type: stdio
    command: uvx
    args: ["agents-fleet", "mcp"]
```
</details>

<details>
<summary><b>Goose</b> — <code>~/.config/goose/config.yaml</code> (Windows <code>%APPDATA%\Block\goose\config\config.yaml</code>)</summary>

```yaml
extensions:
  fleet:
    name: fleet
    type: stdio
    cmd: uvx
    args: [agents-fleet, mcp]
    enabled: true
    timeout: 300
```
</details>

<details>
<summary><b>Codex</b> — <code>~/.codex/config.toml</code></summary>

```toml
[mcp_servers.fleet]
command = "uvx"
args = ["agents-fleet", "mcp"]
```
</details>

<details>
<summary><b>Amp</b> — <code>~/.config/amp/settings.json</code></summary>

```json
{ "amp.mcpServers": { "fleet": { "command": "uvx", "args": ["agents-fleet", "mcp"] } } }
```
</details>

<details>
<summary><b>Mistral Vibe</b> — <code>~/.vibe/config.toml</code></summary>

```toml
[[mcp_servers]]
name = "fleet"
transport = "stdio"
command = "uvx"
args = ["agents-fleet", "mcp"]
```
</details>

**Not supported:** Aider has no MCP support, and ChatGPT's chat reaches only remote MCP
servers; both can still run `fleet` as a shell command. Roo Code has shut down; its
successor is Kilo Code, above.

## Codex's sandbox

In Codex's default `workspace-write` sandbox every fleet command needs your approval:
fleet's whole job is to reach other machines over the network, and its state folder is
outside the workspace. Approve it once instead, in either or both of these ways.

A rule that lets Codex run `fleet` without asking, in `~/.codex/rules/fleet.rules`:

```python
prefix_rule(pattern=["fleet"], decision="allow")
```

A rule matches a command that *starts* with `fleet`. One that only contains it --
`tar czf out.tgz src && fleet ssh box -- 'ls'` -- still runs inside the sandbox, so give
the sandbox the network and fleet's state folder in `~/.codex/config.toml`:

```toml
[sandbox_workspace_write]
network_access = true
# the folders `fleet paths` prints; on Linux, ~/.config/fleet and ~/.local/state/fleet
writable_roots = ["/Users/you/Library/Application Support/fleet"]
```

Without the folder, fleet still runs: it reads its cache, warns once that it cannot
write it, and keeps nothing it measured in that command.

## Skills only

Agents that read `SKILL.md` files can get fleet's skill without any MCP server:

```bash
npx skills add sinolion/fleet          # ✓ — installs into every agent it recognises
```

For Hermes, install the CLI and run `fleet setup --target hermes`, which writes into
Hermes' own home (`%LOCALAPPDATA%\hermes` on Windows, or `$HERMES_HOME`).

## What the MCP server offers

Each tool runs one `fleet` command and returns what it says, so an app without a shell
gets the same answers as an agent with one.

| Tool | Runs |
|---|---|
| `list_machines` | `fleet ls --json`, with `tag` to pick by what a machine has, `fields` for only some keys, `refresh`/`rescan` to measure now |
| `show_machine` | `fleet show NAME --json`, with `rescan` |
| `list_gpu_jobs` | `fleet ps --json`, with `refresh` |
| `list_models` | `fleet models --json`, with `refresh`/`rescan` |
| `run_on_machine` | `fleet ssh NAME -- COMMAND`: its exit code and the last 8000 characters of its output (`stdout_cut` says when there was more), within two minutes |
| `add_machine` | `fleet add "ssh …" --json` |
| `discover_machines` | `fleet ls --discover --json` |
| `doctor_machine` | `fleet doctor [NAME] --json` |
| `wake_machine` | `fleet wake NAME --json`, waiting up to two minutes unless `wait` is false; `via` names the machine that sends the packet |
| `edit_machine` | `fleet edit NAME`: tags, cost, note (on the machine; `local` keeps tags and cost here), address, disks, name, alias |
| `set_machine_paths` | `fleet edit NAME --path KIND=PATH` |
| `set_machine_endpoint` | `fleet edit NAME --endpoint NAME=PORT[/PATH]`: a model server the probe would not find |

Removing a machine, and installing, updating or uninstalling fleet, are not tools: they
are yours to run, in a terminal.

## What `fleet setup` does

With the CLI installed (the one-line installer runs this for you), `fleet setup` teaches
every agent it finds on the machine, and touches nothing else — it never creates a config
directory for an agent you do not use, and in files you own it edits only its own marked
region.

| Agent | What it gets |
|---|---|
| Claude Code | skill: `~/.claude/skills/fleet/SKILL.md` |
| Codex, Gemini CLI, Copilot CLI, OpenCode, Kilo, Amp | one shared skill: `~/.agents/skills/fleet/SKILL.md` — every one of them reads it (checked with each CLI), so it is written once |
| Hermes | skill, in Hermes' own home |
| Claude Desktop, Cursor, VS Code, Windsurf | the MCP server, merged into the app's own config |

Older versions put Codex's skill in `~/.codex/skills` and a region in
`~/.gemini/GEMINI.md`; the next `fleet update` (or `fleet setup --refresh`) moves them,
so no agent ever reads fleet twice.

`fleet setup --project` writes into the current repository instead:
`.claude/skills/fleet/SKILL.md`, or with `--target codex` (or `hermes`, `gemini`) a marked
region in the `AGENTS.md` / `GEMINI.md` those agents read; with `--target cursor` or
`vscode`, the MCP server in the repository's `.cursor/mcp.json` or `.vscode/mcp.json`.
Updating fleet (`fleet update`)
refreshes what every agent is told, on every machine.

**Using the installed CLI instead of `uvx`:** put the full path that `which fleet` prints
as the command, with `["mcp"]` as the args. Desktop apps on macOS do not see
`~/.local/bin` on their PATH, so the bare name `fleet` is not enough there.
