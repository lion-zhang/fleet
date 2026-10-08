# Installing and upgrading

The usual way to install fleet is to ask your agent: "Install fleet from
https://github.com/lion-zhang/fleet". The agent follows [INSTALL.md](../../INSTALL.md),
which tells it to ask whether this machine is a center or a member, to run the installer,
and to ask you for permission if its sandbox blocks the install. This page covers what
the installer does, the other ways to install, upgrading, and removing fleet again.

## What the installer does

```bash
curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"
```

1. Installs [uv](https://docs.astral.sh/uv) if it is missing, then fleet with it — the
   [agents-fleet](https://pypi.org/project/agents-fleet/) package; the command is
   `fleet` — and puts it on the PATH of new shells.
2. Makes this machine the **center** of a new fleet, unless it is in one already, and
   starts the [background service](center.md#the-background-service).
3. Teaches every supported agent installed here to use fleet (`fleet setup`).
4. Says what this machine now is, and what to try next.

**It never replaces an existing fleet.** fleet is installed once per machine and shared
by all its agents. If the installer finds fleet already there — a uv tool, a pipx install,
your own checkout, anything on PATH — it keeps it (an older uv install is upgraded in
place), teaches the agents, and says which copy it kept. Your fleet itself — keys, access
list, inventory — is never touched.

### Options

| `install.sh` | `install.ps1` | Effect |
|---|---|---|
| `--join CODE` | `$env:FLEET_JOIN = "CODE"` | join an existing fleet as a member instead of starting one; `fleet invite` prints the whole line |
| `--no-setup` | `$env:FLEET_NO_SETUP = 1` | install the command only: start or join nothing, teach no agents |
| `--force-core` | `$env:FLEET_FORCE_CORE = 1` | install with uv even though another copy of fleet is here |

Options go after `sh -s --`: `curl -LsSf …/install.sh | sh -s -- --no-setup`.
`FLEET_JOIN=CODE` also works for `install.sh`.

## Other ways to install

Any of these installs the same `fleet`:

```bash
uv tool install agents-fleet && uv tool update-shell
pipx install agents-fleet
```

`uv tool update-shell` matters: on Ubuntu, root's shell does not have `~/.local/bin` on
its PATH, so without it `fleet` is installed and "command not found". Then run
`fleet setup` to teach your agents. On a machine in no fleet, the first `fleet ls`, `show`, `top`, `add`, `invite`, `access`, `setup`, or a bare `fleet center`
(or an MCP tool that runs one) starts a fleet with this machine as center; set
`FLEET_NO_AUTO_CENTER=1` to prevent that.

To install from inside one particular agent — a plugin, an extension, an MCP entry, a
one-click button — see [Agents](agents.md).

To work on fleet itself: `git clone https://github.com/lion-zhang/fleet && uv tool install
--editable ./fleet`. See [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Upgrading

Run the installer again, or `uv tool upgrade agents-fleet` (`pipx upgrade agents-fleet`
for pipx). Your fleet is left as it is. What your agents are told is refreshed by
`fleet setup --refresh`; `fleet ls` mentions it when an agent is reading an older
description.

## Putting fleet on more machines

Machines you only reach *from* the center need nothing installed. For a machine you also
work on — so that its agents see the fleet — the simplest way is an
[invite](add-machines.md#let-the-machine-join-by-itself), which installs fleet there and
joins in one step.

From the center you can also deploy fleet over SSH to machines already in the fleet:

```bash
fleet install gpu-box          # put fleet on a machine that has none
fleet update gpu-box           # update it there
fleet update --all             # update every machine that runs fleet
fleet update                   # update this machine
```

`fleet install` needs the machine's name (on the machine itself, use `fleet update`).
It installs from git: a checkout of the repository's `main` branch in
`~/.local/share/fleet`, or `--repo URL` and `--ref BRANCH` (a branch or a tag).

`fleet update` updates fleet the way it was installed on that machine, and never
replaces a copy you installed yourself:

| fleet there was installed | `fleet update` |
|---|---|
| with uv from PyPI (the installer's way) | `uv tool upgrade agents-fleet` |
| with pipx | `pipx upgrade agents-fleet` |
| by `fleet install` (the checkout in `~/.local/share/fleet`) | fetches the ref and reinstalls from it |
| from a source checkout of yours, or some other way | kept as it is, and says so |

`--repo` or `--ref` asks for git by name, and then git it is. Both commands forward your
SSH agent for a clone, so the machine fetches as you and no credential is left on it
(`--no-forward-agent` turns that off). Each also refreshes what the agents on that
machine are told, and restarts the center's background service if it ran.

## Removing fleet

```bash
fleet uninstall            # leave the fleet, remove fleet from your agents and the service
fleet uninstall --purge    # ...and delete fleet's files here: key, inventory, cache, logs
```

In order, it:

1. takes this machine out of its fleet: on a member, the fleet's keys come off this
   machine (the center still lists it until `fleet rm NAME` there); an empty center
   just forgets its fleet;
2. removes the skills and MCP entries fleet gave your agents;
3. removes the background service;
4. with `--purge`, deletes fleet's own files, by name -- nothing else in those folders.

It asks first (`--yes` skips that, and is needed without a terminal), and ends by
printing the command that removes the program itself -- `uv tool uninstall agents-fleet`,
or `pipx uninstall agents-fleet` -- for you to run: a program cannot reliably remove
itself while it runs.

The center of a fleet with other machines in it refuses: its key is on every one of
them and only it can take it off. Hand the role on (`fleet center NAME`) or end the
fleet (`fleet center --dissolve`, which removes every key first; see
[The center](center.md#leaving-and-ending)), then run `fleet uninstall`.
