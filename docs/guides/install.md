# Installing and upgrading

The usual way to install fleet is to ask your agent: "Install fleet from
https://github.com/sinolion/fleet". The agent follows [INSTALL.md](https://github.com/sinolion/fleet/blob/main/INSTALL.md),
which tells it to run the installer and to ask you for permission if its sandbox blocks
the install. There is nothing to choose: every machine running fleet is installed the
same way. This page covers what the installer does, the other ways to install,
upgrading, and removing fleet again.

## What the installer does

```bash
curl -LsSf https://raw.githubusercontent.com/sinolion/fleet/main/install.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/sinolion/fleet/main/install.ps1 | iex"
```

1. Installs [uv](https://docs.astral.sh/uv) if it is missing, then fleet with it — the
   [agents-fleet](https://pypi.org/project/agents-fleet/) package; the command is
   `fleet` — and puts it on the PATH of new shells.
2. Teaches every supported agent installed here to use fleet (`fleet setup`), which also
   records this machine in fleet's list.
3. Says what to try next: add a machine by its ssh command, or look in your ssh config
   and tailnet with `fleet ls --discover`.

It needs no root, asks no questions, and starts nothing in the background.

**It never replaces an existing fleet.** fleet is installed once per machine and shared
by all its agents. If the installer finds fleet already there — a uv tool, a pipx install,
your own checkout, anything on PATH — it keeps it (an older uv install is upgraded in
place), teaches the agents, and says which copy it kept. fleet's files — your list of
machines, its cache — are never touched.

### Options

| `install.sh` | `install.ps1` | Effect |
|---|---|---|
| `--no-setup` | `$env:FLEET_NO_SETUP = 1` | install the command only: teach no agents |
| `--force-core` | `$env:FLEET_FORCE_CORE = 1` | install with uv even though another copy of fleet is here |

Options go after `sh -s --`: `curl -LsSf …/install.sh | sh -s -- --no-setup`.

`--join CODE` and `FLEET_JOIN`, from fleet 0.5, are no longer needed: there is no fleet to
join. The installer says so and ignores them.

## Other ways to install

Any of these installs the same `fleet`:

```bash
uv tool install agents-fleet && uv tool update-shell
pipx install agents-fleet
```

`uv tool update-shell` matters: on Ubuntu, root's shell does not have `~/.local/bin` on
its PATH, so without it `fleet` is installed and "command not found". Then run
`fleet setup` to teach your agents — or just start using it: the first `fleet ls`,
`show`, `top`, `add` or `setup` on a machine (or an MCP tool that runs one) records the
machine and teaches the agents found there, once, and says so in a line. Set
`FLEET_NO_FIRST_RUN=1` to skip that.

To install from inside one particular agent — a plugin, an extension, an MCP entry, a
one-click button — see [Agents](agents.md).

To work on fleet itself: `git clone https://github.com/sinolion/fleet && uv tool install
--editable ./fleet`. See [CONTRIBUTING.md](https://github.com/sinolion/fleet/blob/main/CONTRIBUTING.md).

## Upgrading

Run the installer again, or `fleet update`, or `uv tool upgrade agents-fleet`
(`pipx upgrade agents-fleet` for pipx). Your list of machines is left as it is. What your
agents are told is refreshed by the installer and `fleet update`, or by
`fleet setup --refresh`; `fleet ls` mentions it when an agent is reading an older
description.

**From 0.5:** fleet 0.6 dropped the center, members, invites and access lists. Nothing is
required of you, but your list of machines starts afresh, and 0.5 left some things on
your machines that you may remove. See [Upgrading from 0.5](upgrading-from-0.5.md).

## Putting fleet on more machines

The machines you only watch need nothing installed. Put fleet on a machine only if you
also work there — so that its agents can use fleet too. Install it there the same way, or
from a machine that already watches it:

```bash
fleet install gpu-box          # put fleet on a machine that has none
fleet update gpu-box           # update it there
fleet update --all             # update every machine that runs fleet
fleet update                   # update this machine
```

`fleet install` needs a machine's name: the one you run it on already has fleet, and
`fleet update` updates it.
It installs from git: a checkout of the repository's `main` branch in
`~/.local/share/fleet`, or `--repo URL` and `--ref BRANCH` (a branch or a tag). When the
repository's URL is an ssh one (`git@github.com:you/fleet.git`), it forwards your SSH
agent for the clone, so the machine fetches as you and no credential is left on it. An
https URL needs nothing of yours, so the agent is not forwarded: whoever is root on the
machine could use it while the install runs. `--forward-agent` and `--no-forward-agent`
decide it either way.

A machine with fleet on it starts with its own empty list: it watches the machines *its*
ssh reaches, which you add there. To give it the same list as this one, copy
`machines.yaml` (`fleet paths` shows where), or share it with your dotfiles.

`fleet update` updates fleet the way it was installed on that machine, and never
replaces a copy you installed yourself:

| fleet there was installed | `fleet update` |
|---|---|
| with uv from PyPI (the installer's way) | `uv tool upgrade agents-fleet` |
| with pipx | `pipx upgrade agents-fleet` |
| by `fleet install` (the checkout in `~/.local/share/fleet`) | fetches the ref and reinstalls from it |
| under its name before 0.5, `fleet-broker` or `agent-fleet` | replaced by `agents-fleet` from PyPI, or rebuilt from fleet's checkout if the machine has one |
| from a source checkout of yours, or some other way | kept as it is, and says which version it found and that it did not update it |

`--repo` or `--ref` asks for git by name, and then git it is. Each also refreshes what
the agents on that machine are told, and takes off fleet 0.5's background service if one
is still there.

On Windows, `fleet update` of this machine runs in the background once the command has
exited, since Windows cannot replace a program while it runs; see
[Windows](windows.md#running-fleet-on-windows).

### From before 0.5

fleet was called `fleet-broker` until 0.5, and `agent-fleet` for a while after. Running
the one-line installer on such a machine, or `fleet update NAME` for it, moves it to
`agents-fleet` and removes the old tool, so two programs never both claim the `fleet`
command.

## Removing fleet

```bash
fleet uninstall            # remove fleet from your agents
fleet uninstall --purge    # ...and delete fleet's files here: list of machines, cache, logs
```

It:

1. removes the skills and MCP entries fleet gave your agents;
2. with `--purge`, deletes fleet's own files, by name — nothing else in those folders.

Nothing changes on the machines fleet watched. It asks first (`--yes` skips that, and is
needed without a terminal), and ends by printing the command that removes the program
itself — `uv tool uninstall agents-fleet`, or `pipx uninstall agents-fleet` — for you to
run: a program cannot reliably remove itself while it runs.

`--purge` deletes 0.6's files only. What fleet 0.5 kept there is listed in
[Upgrading from 0.5](upgrading-from-0.5.md#files-in-fleets-folders).
