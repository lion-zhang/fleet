# Contributing to fleet

Issues and pull requests are welcome. This page covers how the code is laid out, how to
test a change, and the few rules that are not up for debate.

## Before you change anything

fleet has three layers, and that design is settled
([docs/design/layers.md](docs/design/layers.md)):

- **Core** — the `fleet` CLI, plus its state and configuration. Installed once per
  machine. A person can always run it by hand.
- **Skill** — text that tells an agent how to use the CLI. No code.
- **MCP** — runs the same CLI for agents that cannot run commands. A thin wrapper,
  never a second implementation.

What follows from it:

- A new capability goes into the CLI first. The skill then describes it, and MCP calls
  the CLI for it.
- Reads run in parallel and never wait; state files are only replaced atomically.
- Writes go through one queue and apply to the state as it is now — never save a copy
  loaded earlier.
- The installer never overwrites an existing core or touches fleet's state.
- fleet manages no access: it connects as the user's own ssh does and writes no keys.
  There is one install mode; don't reintroduce "center" or "member".

## Setting up

```bash
git clone https://github.com/lion-zhang/fleet && cd fleet
uv sync --all-extras
uv run fleet --help
```

To use your checkout as your everyday `fleet`: `uv tool install --editable .`.

## Layout

| Path | What is there |
|---|---|
| `src/fleet/cli.py` | every command |
| `src/fleet/state/` | the list of machines, the write queue, the telemetry store |
| `src/fleet/ops/` | multi-step operations: discovery, doctor, a machine's notes, first run, uninstall |
| `src/fleet/ssh/` | building ssh commands, and the words for putting your key on a machine |
| `src/fleet/probe/` | the script piped to a machine to measure it (`payload.sh`, `payload.ps1`) and its parser |
| `src/fleet/agents/` | the skill text (`usage.py`) and where each agent reads it (`registry.py`) |
| `src/fleet/mcpserver.py` | the MCP server |
| `src/fleet/notes.py` | a machine's own `machine.yaml`: read, checked, edited |
| `install.sh`, `install.ps1` | the installers |
| `tests/` | unit tests; `tests/e2e/` drives a real install |

## Testing

```bash
uv run pytest -q
```

CI runs the suite on Linux, macOS and Windows, with Python 3.12 and 3.13
(`.github/workflows/test.yml`).

**End to end.** `tests/e2e/host.py` installs fleet with the real installer and runs
every command against it — over ssh to the machine itself too, with the runner's own key,
so probing, a machine's notes and `fleet ssh` go through that OS's sshd and shell. It also drives `fleet top` and `fleet ssh` at a real terminal (`tests/e2e/term.py`:
a pty, or ConPTY on Windows), because agents run fleet without a terminal and people run
it with one, and both must work. CI runs it on each OS for changes to `src/`, the
installers or the e2e files (`.github/workflows/e2e.yml`); you can also start it from the
Actions tab.

## Generated files

These are written by scripts and checked by tests; edit the source, then regenerate:

| Files | Source | Regenerate with |
|---|---|---|
| `skills/fleet/SKILL.md`, the plugin and extension manifests, `server.json`, `mcpb/` | `src/fleet/agents/usage.py`, `pyproject.toml` | `uv run python scripts/build_dist.py` |
| `docs/reference/cli.md` | the commands' help text in `src/fleet/cli.py` | `uv run python scripts/build_dist.py` |
| the screenshots in `docs/assets/` | the renderer | `uv run python scripts/readme_screenshots.py` |
| the demo, `docs/assets/fleet-demo.svg` | the renderer and its script | `uv run python scripts/readme_demo.py` |

`tests/test_dist.py` fails when a committed copy differs from what the script writes.

## The documentation site

`docs/` is also published as a website, [agents-fleet.dev](https://agents-fleet.dev/),
by `.github/workflows/docs.yml` on every push to main. The pages are the same Markdown files
GitHub shows; `mkdocs.yml` only adds the sidebar. A new page goes in two places: the index,
`docs/README.md`, and `nav:` in `mkdocs.yml`.

```bash
uv run --group docs mkdocs serve      # http://127.0.0.1:8000, rebuilt as you edit
uv run --group docs mkdocs build      # what CI runs: a broken link or anchor fails it
```

Link to files outside `docs/` (`INSTALL.md`, `SECURITY.md`) by their GitHub URL, since the
site has no copy of them.

## Supporting another agent

Most agents are one entry in `AGENTS` (or `MCP_CLIENTS` for apps without a shell) in
`src/fleet/agents/registry.py`: where the agent reads skills or its MCP config, and how to
tell it is installed. Add it to the table in [docs/guides/agents.md](docs/guides/agents.md)
too.

## Releasing

Publishing a GitHub release tagged `vX.Y.Z` (matching the version in `pyproject.toml`)
runs `.github/workflows/release.yml`: it checks the generated files are current, builds,
uploads to PyPI as `agents-fleet`, and attaches the wheel and the Claude Desktop bundle to
the release. `.github/workflows/mcp-registry.yml` then lists the version in the MCP
Registry.
