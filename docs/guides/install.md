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
On a machine with no fleet it installs the release from PyPI, as the one-line installer
does (`uv tool install agents-fleet`, fetching uv first if the machine has none).

Neither command replaces a fleet that is already there. Both update it the way it was
installed on that machine:

| fleet there was installed | `fleet update` / `fleet install` |
|---|---|
| with uv from PyPI (the installer's way) | `uv tool upgrade agents-fleet` |
| with pipx | `pipx upgrade agents-fleet` |
| from fleet's checkout in `~/.local/share/fleet` (what older releases of `fleet install` made) | fetches the ref and reinstalls from it |
| under its name before 0.5, `fleet-broker` or `agent-fleet` | replaced by `agents-fleet` from PyPI, or rebuilt from fleet's checkout if the machine has one |
| from a source checkout of yours (editable or not), or some other way | kept as it is: reported as skipped, with the version it found and why it left it |
| not at all | `fleet install` installs the release; `fleet update` skips the machine |

`--repo URL` or `--ref REF` (a branch or a tag, including `--ref main`) asks for git by
name, and then every machine is installed from a checkout of that ref in
`~/.local/share/fleet`, whatever was there before. For that clone both commands forward
your SSH agent, so the machine fetches as you and no credential is left on it
(`--no-forward-agent` turns that off). Updating never touches fleet's state: the
inventory, keys and access list stay as they are. Each command also refreshes what the
agents on that machine are told, and restarts the center's background service if it ran.

### From before 0.5

fleet was called `fleet-broker` until 0.5, and `agent-fleet` for a while after. A
machine installed under either name is updated across the rename only by a center
running a release newer than 0.5.1: run `fleet update NAME` (or `--all`) there. Older
releases updated such a machine only if it had fleet's checkout in
`~/.local/share/fleet`, and otherwise said "kept as it is" and left it on the old
version. On the machine itself, the one-line installer does the same move.

A `fleet-center` service left by an old install can linger and block the new one. On
Linux and macOS installing the service replaces it. On Windows a task an old version
registered to run elevated can be removed only from an administrator shell: see
[Windows](windows.md#running-fleet-on-windows).

## Moving or reinstalling a center: keep its key

The center's key is `id_ed25519` in fleet's configuration folder
(`~/.config/fleet` on Linux, `~/Library/Application Support/fleet` on macOS,
`%LOCALAPPDATA%\fleet` on Windows). It is the machine's identity: every machine in the
fleet holds its public half in `authorized_keys`, and nothing else can stand in for it.
Rebuild the center with that key restored, and every machine still lets it in; lose it,
and each machine has to be visited by hand.

So before `fleet uninstall --purge`, or reinstalling the OS, copy the folder somewhere
safe, along with what `fleet center --export` prints (the access list and its pins), and put `id_ed25519` and
`id_ed25519.pub` back in place before running fleet again. Never generate a new one over
it. To move the role to another machine instead, hand it over with `fleet center NAME`;
that keeps every machine's trust intact without copying a key anywhere.

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
