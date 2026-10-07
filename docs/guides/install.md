# Installing and upgrading

The usual way to install fleet is to ask your agent: "Install fleet from
https://github.com/lion-zhang/fleet". This page covers what that does, the other ways to
install, upgrading, and removing fleet again.

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
`fleet setup` to teach your agents; the first fleet command on a machine in no fleet
starts one with this machine as center (set `FLEET_NO_AUTO_CENTER=1` to prevent that).

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

These install from git — a checkout of the repository's `main` branch in
`~/.local/share/fleet`, or `--repo URL` and `--ref BRANCH` — rather than from PyPI.
They forward your SSH agent for the clone, so the machine fetches as you and no
credential is left on it (`fleet install --no-forward-agent` turns that off). Each also
refreshes what the agents on that machine are told.

## Removing fleet

On a member, take the machine out of the fleet first, then remove the program:

```bash
fleet center --leave           # removes this fleet's keys from this machine
fleet setup --uninstall        # removes the skills and MCP entries fleet installed
uv tool uninstall agents-fleet # or: pipx uninstall agents-fleet
```

On the center, `fleet center --dissolve` takes the whole fleet down first (see
[The center](center.md#leaving-and-ending)); hand the role over instead if the fleet
should live on. `fleet paths` shows where fleet keeps its files, if you want them gone
too.
