# Installing fleet — instructions for coding agents

You are a coding agent, and the user asked you to install fleet. These are the steps.
(People can read along: [Installing and upgrading](docs/guides/install.md) is the guide
for doing it by hand.)

fleet is a command-line tool, `fleet`, installed **once per machine** and shared by every
agent on it. Installing it also teaches every supported agent on this machine to use it.

## 1. Ask which mode this machine is for

Ask the user **before you install anything**, unless they already said:

- **center** — this machine starts a new fleet and keeps it: the list of machines, the key
  that reaches them, who may reach what. Recommend a machine that can reach all their
  devices and is online most of the time. This is the usual first install.
- **member** — this machine joins a fleet the user already has. They need an invite: on
  that fleet's center, `fleet invite NAME` prints a code (`fleet1:…`) or the whole install
  line. Ask them for it.

**If `fleet --version` already works here**, fleet is installed: do not reinstall it.
Find out what this machine is, without changing anything:

```bash
FLEET_NO_AUTO_CENTER=1 fleet center --json     # PowerShell: $env:FLEET_NO_AUTO_CENTER=1 first
```

`"role": "center"` or `"member"`: it is in a fleet already; run `fleet setup` to teach the
agents here, and report. `"role": ""`: it is in no fleet yet; ask the question above,
then run `fleet setup` for a center or `fleet join CODE && fleet setup` for a member.
On a machine in none, plain `fleet center`, `fleet ls`, `show`, `top`, `add`, `invite`,
`access` and `setup` start a new fleet, so do not run them before you know.

## 2. Run the installer

The installer needs no terminal and asks no questions. Choose one:

| | Center | Member |
|---|---|---|
| macOS, Linux | `curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh \| sh` | `curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh \| sh -s -- --join CODE` |
| Windows (PowerShell) | `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 \| iex"` | the same, after `$env:FLEET_JOIN = "CODE"` |

**If piping a script into a shell is not allowed** — by your rules, your sandbox or the
user — install the same thing from PyPI instead:

```bash
uv tool install agents-fleet && uv tool update-shell
fleet setup                    # center: starts the fleet, teaches the agents
fleet join CODE && fleet setup # member
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
  `~/.claude/skills` and `~/.agents/skills`;
- on a center, a background service run as the user: a systemd user unit, a launchd
  agent, or a scheduled task.

It never needs root, and it never replaces a fleet that is already installed.

## 3. Report the result

The installer ends with what this machine now is — center or member — and what to try
next. Pass that on to the user in their words, and offer to add their first machine.
Point out:

- **Start a new session.** Agents load skills when a session starts, so fleet's skill is
  there from the next conversation on — yours included.
- **If `fleet` is not found** in a new shell, the user needs to open a new terminal, once.
- **If the installer says `no service manager for this user here`** (a container, most
  GPU rentals), the center is not listening for members and invites. Tell the user, and
  offer to start it: `nohup fleet center --listen > ~/fleet-center.log 2>&1 &`.

## Rules

- **Never install fleet unasked.** It makes the machine the center of a new fleet.
- **Never type or ask for a password.** fleet never needs one to install. Later, adding
  a password-only machine is the one step the user does themselves, in a terminal.
- **Invite codes are secrets** for the few minutes they are valid: use the one you are
  given, and do not repeat it back.

## Then

Once installed, the commands are described by fleet's skill and by `fleet --help`. The
[documentation](docs/README.md) covers everything else.
