# fleet in every agent

The quickest way is still the one-line installer on the machine you work from. It installs
fleet and teaches every agent it finds there, in one step:

```bash
curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh
```

**Several agents on one machine is the normal case.** fleet is installed once per
machine — one command, one inventory, one set of keys — and each agent only gets a small
skill or MCP entry that points at it. Claude Code, Codex and a desktop app on the same
machine all see the same fleet, and can use it at the same time: changes are written
under a lock, so two agents editing at once never undo each other. Added an agent later?
`fleet setup` teaches it.

This page is for adding fleet from inside one agent, or to an agent the installer does not
know. Every entry runs the same `fleet` from PyPI ([agent-fleet](https://pypi.org/project/agent-fleet/)),
so all your agents share one fleet. MCP entries need only [uv](https://docs.astral.sh/uv/)
(`uvx`); skills and plugins also need the `fleet` command, and say how to get it if it is
missing.

> **Center or member.** On a machine in no fleet yet, the first use of fleet makes it the
> **center** of a new fleet, whichever way it was installed. If this machine should be a
> **member** of a fleet you already have, paste the line `fleet invite` prints on that
> fleet's center *first*, then add the agent below.

**✓** = the command was run here and the agent registered (or connected to) the server.
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
| Claude Code | `/plugin marketplace add lion-zhang/fleet` then `/plugin install fleet@fleet`<br>(shell: `claude plugin marketplace add lion-zhang/fleet && claude plugin install fleet@fleet`) | ✓ |
| Codex | `codex plugin marketplace add lion-zhang/fleet && codex plugin add fleet@fleet` | ✓ |
| GitHub Copilot CLI | `copilot plugin marketplace add lion-zhang/fleet && copilot plugin install fleet@fleet` | ✓ |
| Gemini CLI | `gemini extensions install https://github.com/lion-zhang/fleet` — the skill and the MCP server | ✓ |

## One command

These register fleet's MCP server, launched as `uvx agent-fleet mcp`.

| Agent | Command | |
|---|---|---|
| Claude Code | `claude mcp add --scope user fleet -- uvx agent-fleet mcp` | ✓ |
| Codex (CLI, IDE, ChatGPT app) | `codex mcp add fleet -- uvx agent-fleet mcp` | ✓ |
| GitHub Copilot CLI | `copilot mcp add fleet -- uvx agent-fleet mcp` | ✓ |
| Amp | `amp mcp add fleet -- uvx agent-fleet mcp` | ✓ |
| OpenCode | `opencode mcp add fleet -- uvx agent-fleet mcp` | ✓ |
| Qwen Code | `qwen mcp add fleet uvx agent-fleet mcp` | ✓ |
| Auggie (Augment CLI) | `auggie mcp add fleet -- uvx agent-fleet mcp` | ✓ |
| Factory Droid | `droid mcp add fleet "uvx agent-fleet mcp"` | ✓ |
| Kiro CLI | `kiro-cli mcp add --name fleet --scope global --command uvx --args agent-fleet --args mcp` | ✓ |
| Mistral Vibe | `vibe mcp add fleet --transport stdio --command uvx --arg agent-fleet --arg mcp` | ✓ |
| VS Code (Copilot agent mode) | `code --add-mcp '{"name":"fleet","command":"uvx","args":["agent-fleet","mcp"]}'` | docs |
| Windsurf / Devin Desktop | `devin mcp add fleet -s user -- uvx agent-fleet mcp` | docs |
| Jan | `jan cli mcp add fleet --command uvx --arg agent-fleet --arg mcp --active` | docs |
| Crush | `echo 'mcp add fleet --command uvx --args agent-fleet --args mcp' >> ~/.config/crush/crushrc` | docs |

## One click

| Agent | Link |
|---|---|
| Cursor | [Add to Cursor](https://cursor.com/en/install-mcp?name=fleet&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJhZ2VudC1mbGVldCIsIm1jcCJdfQ%3D%3D) |
| VS Code | [Install in VS Code](https://insiders.vscode.dev/redirect/mcp/install?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agent-fleet%22%2C%22mcp%22%5D%7D) · [VS Code Insiders](https://insiders.vscode.dev/redirect/mcp/install?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agent-fleet%22%2C%22mcp%22%5D%7D&quality=insiders) |
| Visual Studio | [Install in Visual Studio](https://vs-open.link/mcp-install?%7B%22name%22%3A%22fleet%22%2C%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agent-fleet%22%2C%22mcp%22%5D%7D) |
| Kiro | [Add to Kiro](https://kiro.dev/launch/mcp/add?name=fleet&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22agent-fleet%22%2C%22mcp%22%5D%7D) |
| LM Studio | [Add to LM Studio](https://lmstudio.ai/install-mcp?name=fleet&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJhZ2VudC1mbGVldCIsIm1jcCJdfQ%3D%3D) |
| Goose | `goose://extension?cmd=uvx&arg=agent-fleet&arg=mcp&id=fleet&name=fleet&description=fleet&timeout=300` (paste into a browser) |
| Claude Desktop | download `fleet.mcpb` from the [latest release](https://github.com/lion-zhang/fleet/releases/latest) and open it ✓ |

These links are built from each agent's documented format; the Claude Desktop bundle was
validated, packed and run here.

## Config files

For everything else, add fleet to the agent's config. Most agents read the same shape:

```json
{
  "mcpServers": {
    "fleet": { "command": "uvx", "args": ["agent-fleet", "mcp"] }
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
| Msty Studio | Toolbox → Tools → Add New Tool → *STDIO / JSON* | the inner object only: `{"command": "uvx", "args": ["agent-fleet", "mcp"]}` |

The agents below use their own shapes.

<details>
<summary><b>VS Code</b> — user <code>mcp.json</code> (<i>MCP: Open User Configuration</i>), or <code>.vscode/mcp.json</code> in a project</summary>

```json
{ "servers": { "fleet": { "type": "stdio", "command": "uvx", "args": ["agent-fleet", "mcp"] } } }
```

Visual Studio reads the same shape from `%USERPROFILE%\.mcp.json`.
</details>

<details>
<summary><b>OpenCode</b> (<code>~/.config/opencode/opencode.json</code>) and <b>Kilo Code</b> (<code>~/.config/kilo/kilo.json</code>) — ✓</summary>

```json
{ "mcp": { "fleet": { "type": "local", "command": ["uvx", "agent-fleet", "mcp"], "enabled": true } } }
```
</details>

<details>
<summary><b>Zed</b> — <code>~/.config/zed/settings.json</code> (Windows <code>%APPDATA%\Zed\settings.json</code>)</summary>

```json
{ "context_servers": { "fleet": { "command": "uvx", "args": ["agent-fleet", "mcp"], "env": {} } } }
```
</details>

<details>
<summary><b>Continue</b> — <code>~/.continue/config.yaml</code> (agent mode only)</summary>

```yaml
mcpServers:
  - name: fleet
    type: stdio
    command: uvx
    args: ["agent-fleet", "mcp"]
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
    args: [agent-fleet, mcp]
    enabled: true
    timeout: 300
```
</details>

<details>
<summary><b>Codex</b> — <code>~/.codex/config.toml</code></summary>

```toml
[mcp_servers.fleet]
command = "uvx"
args = ["agent-fleet", "mcp"]
```
</details>

<details>
<summary><b>Amp</b> — <code>~/.config/amp/settings.json</code></summary>

```json
{ "amp.mcpServers": { "fleet": { "command": "uvx", "args": ["agent-fleet", "mcp"] } } }
```
</details>

<details>
<summary><b>Mistral Vibe</b> — <code>~/.vibe/config.toml</code></summary>

```toml
[[mcp_servers]]
name = "fleet"
transport = "stdio"
command = "uvx"
args = ["agent-fleet", "mcp"]
```
</details>

**Not supported:** Aider has no MCP support, and ChatGPT's chat reaches only remote MCP
servers; both can still run `fleet` as a shell command. Roo Code has shut down; its
successor is Kilo Code, above.

## Skills only

Agents that read `SKILL.md` files can get fleet's skill without any MCP server:

```bash
npx skills add lion-zhang/fleet          # ✓ — installs into every agent it recognises
```

For Hermes, install the CLI and run `fleet setup --target hermes`, which writes into
Hermes' own home (`%LOCALAPPDATA%\hermes` on Windows, or `$HERMES_HOME`).

## What `fleet setup` does

With the CLI installed (the one-line installer runs this for you), `fleet setup` teaches
every agent it finds on the machine, and touches nothing else — it never creates a config
directory for an agent you do not use, and in files you own it edits only its own marked
region.

| Agent | What it gets |
|---|---|
| Claude Code | skill: `~/.claude/skills/fleet/SKILL.md` |
| Codex | skill: `~/.codex/skills/fleet/SKILL.md` |
| Gemini CLI | a marked region in `~/.gemini/GEMINI.md` |
| Hermes | skill, in Hermes' own home |
| Claude Desktop, Cursor, VS Code, Windsurf | the MCP server, merged into the app's own config |

`fleet setup --project` writes into the current repository instead:
`.claude/skills/fleet/SKILL.md`, or with `--target codex` (or `hermes`, `gemini`) a marked
region in the `AGENTS.md` / `GEMINI.md` those agents read. Updating fleet (`fleet update`)
refreshes what every agent is told, on every machine.

**Using the installed CLI instead of `uvx`:** put the full path that `which fleet` prints
as the command, with `["mcp"]` as the args. Desktop apps on macOS do not see
`~/.local/bin` on their PATH, so the bare name `fleet` is not enough there.
