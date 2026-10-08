# fleet — notes for agents working on this repo

## Design decisions — do not revisit

fleet has three layers (full text: [docs/design/layers.md](docs/design/layers.md)):

- **Core** — the `fleet` CLI plus its state and configuration, installed once per
  machine. People can always run it by hand.
- **Skill** — a prompt telling an agent how to use the CLI properly. Text only.
- **MCP** — runs the same CLI for agents that cannot run commands. A thin wrapper,
  never a second implementation.

Consequences:
- Add a capability to the CLI first; the skill describes it, MCP calls the CLI for it.
- Reads run in parallel and never wait; state files are only replaced atomically.
- Writes go through one queue and apply to the state as it is now — never save a copy
  loaded earlier. Nobody blocks on anybody.
- The installer never overwrites an existing core or touches fleet's state.

## Working here

- Tests: `uv run pytest -q`; CI runs them on Linux, macOS and Windows.
- End to end: `tests/e2e/host.py` runs every command against a real install, on each OS
  (`.github/workflows/e2e.yml`, also from the Actions tab). It drives `fleet top` and
  `fleet ssh` at a real terminal (`tests/e2e/term.py`: a pty, or ConPTY on Windows) —
  agents run fleet with no terminal, people with one, and both must be tested.
- Generated files, never hand-edited: `skills/fleet/SKILL.md`, the plugin/extension
  manifests and `docs/reference/cli.md` (`uv run python scripts/build_dist.py`), the README screenshots
  (`uv run python scripts/readme_screenshots.py`) and the demo
  (`uv run python scripts/readme_demo.py`).
- The two install modes are **center** and **member**; use those words.
