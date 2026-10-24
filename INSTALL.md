# Installing fleet — instructions for coding agents

You are a coding agent, and the user asked you to install fleet. These are the steps.
(People can read along: [Installing and upgrading](docs/guides/install.md) is the guide
for doing it by hand.)

fleet is a command-line tool, `fleet`, installed **once per machine** and shared by every
agent on it. Installing it also teaches every supported agent on this machine to use it.
There is nothing to choose: every machine running fleet is the same, and looks at the
machines its own SSH reaches.

**If `fleet --version` already works here**, fleet is installed: do not reinstall it.
Run `fleet setup` to teach the agents here, and report.

## 1. Run the installer

The installer needs no terminal and asks no questions.

| | |
|---|---|
| macOS, Linux | `curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh \| sh` |
| Windows (PowerShell) | `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 \| iex"` |

**If piping a script into a shell is not allowed** — by your rules, your sandbox or the
user — install the same thing from PyPI instead:

```bash
uv tool install agents-fleet && uv tool update-shell
fleet setup                    # teaches the agents here
```

(`uv` itself installs with `curl -LsSf https://astral.sh/uv/install.sh | sh`, or
`pip install uv`. `pipx install agents-fleet` also works.)

**What the installer touches**, so you can ask for the right permissions up front. If
your sandbox blocks any of this, ask the user to allow it; do not leave a half install.

- the network: GitHub, PyPI, and astral.sh if uv is missing;
- `~/.local/bin`, and uv's tool folder;
- fleet's folders: `~/.config/fleet` and `~/.local/state/fleet`, or
  `~/Library/Application Support/fleet` on macOS, or `%LOCALAPPDATA%\fleet` on Windows;
- the skill and MCP folders of agents already installed here, such as
  `~/.claude/skills` and `~/.agents/skills`.

It never needs root, starts no background service, and never replaces a fleet that is
already installed.

## 2. Report the result

The installer ends with what to try next. Pass that on to the user in their words, and
offer to add their first machine — or to look in their `~/.ssh/config` and on their
tailnet for machines they can already reach (`fleet ls --discover`). Point out:

- **Start a new session.** Agents load skills when a session starts, so fleet's skill is
  there from the next conversation on — yours included.
- **If `fleet` is not found** in a new shell, the user needs to open a new terminal, once.

## Rules

- **Never install fleet unasked.**
- **Never type or ask for a password.** fleet never needs one. A machine that only takes
  a password needs the user's key on it once; `fleet doctor NAME` prints how.
- fleet manages no access: it connects exactly as the user's own ssh does. Do not offer
  to change anyone's keys or SSH policy; say what `fleet doctor` says, and let the user
  decide.

## Then

Once installed, the commands are described by fleet's skill and by `fleet --help`. The
[documentation](docs/README.md) covers everything else.
