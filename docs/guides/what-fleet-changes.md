# What fleet changes on your machines

Very little, and nothing about who may log in. fleet manages no access: it never writes
`authorized_keys`, never creates a key, and runs no service. This page is the whole list.

## On the machines you watch

fleet installs nothing on them. A reading is one script sent over one ssh connection; it
reads what the machine has and what is free, and writes only these:

| What | Where | Why |
|---|---|---|
| temporary files | wherever `mktemp` says (usually `/tmp`), Linux and macOS | the script and its output, and what a model server answered; deleted when the reading finishes. On Windows the script is piped to PowerShell and nothing is written. |
| a small cache | `~/.cache/fleet` (`$XDG_CACHE_HOME/fleet`), Linux and macOS | folder sizes, worked out by `du` in the background so a probe never waits for it, and the software list, in a folder named for the machine: nodes that share a home keep their own. Refreshed every six hours, or with `--rescan`. |
| `machine.yaml` | `~/.config/fleet/machine.yaml`; Windows `%LOCALAPPDATA%\fleet\machine.yaml` | only when you run `fleet edit NAME` with `--path`, `--endpoint`, `--note`, or `--tag`/`--cost` without `--local`: where it keeps models and data, servers to ask, its tags, cost and a note. [More](resources.md#machineyaml) |
| `device-id` | `~/.config/fleet/device-id` (macOS `~/Library/Application Support/fleet`, Windows `%LOCALAPPDATA%\fleet`) | only on a machine cloned from another's image, so the two are told apart |

That is everything, unless you put fleet itself on a machine with `fleet install NAME`
([Installing](install.md#putting-fleet-on-more-machines)). `fleet rm` changes nothing on the machine, and nothing needs undoing
there: delete `~/.cache/fleet` if you like, and keep or delete `machine.yaml`, which is
yours.

## On the machine you run fleet on

| What | Where |
|---|---|
| **the program** | installed with [uv](https://docs.astral.sh/uv) as `agents-fleet` (`uv tool list` shows it). The installer installs uv first if it is missing, and lets uv add its folder of tools to your `PATH`. |
| **your list of machines** | `machines.yaml` in fleet's configuration folder — `~/.config/fleet` on Linux, `~/Library/Application Support/fleet` on macOS, `%LOCALAPPDATA%\fleet` on Windows |
| **a cache of readings** | `cache.db` in fleet's state folder — `~/.local/state/fleet` on Linux, the same folder as above elsewhere. Disposable. |
| **`~/.ssh/known_hosts`** | ssh records the host key of a machine fleet reaches for the first time (`StrictHostKeyChecking=accept-new`, which overrides `yes` or `ask` in your ssh config: an agent has no terminal to answer ssh's question). A key that *changed* is still refused. |
| **open ssh connections** | Linux and macOS: one per machine, kept for two minutes after a command so the next one does not dial again (`ControlPersist`), then closed by ssh itself. Their sockets are in `/tmp/fleet-UID`, readable by you alone. |
| **your agents** | a skill file (for example `~/.claude/skills/fleet/SKILL.md`), or an entry in an app's MCP settings. Only for agents found on this machine; `fleet setup` prints each file it writes, and keeps the rest of every settings file as it was. |

Both folders are readable by you alone. Every file is listed in
[Configuration](../reference/configuration.md#files); `fleet paths` shows where they are.

## On the network

fleet opens ssh connections from this machine to the machines you added, as you, and
nothing else. It listens on no port, opens no tunnel and asks no third party to relay
anything. `fleet ls --discover` runs `tailscale status` locally; it does not call
Tailscale's servers itself.

fleet reaches the internet only when you install or update it: from GitHub, from PyPI,
and from astral.sh for uv. No telemetry, no account, no update check.

## What fleet never does

- **Ask for or store a password.** ssh runs in batch mode, so it never prompts. The one
  password ever typed is yours, into `ssh-copy-id`, if you accept fleet's offer to put
  your key on a machine.
- **Touch `authorized_keys`**, or any other key. fleet has no key of its own.
- **Show a key or a password to your agent.** The agent runs `fleet ssh NAME`. It may
  see a machine's address.
- **Run as root**, or ask for sudo.
- **Delete files by pattern.** `fleet uninstall --purge` deletes fleet's files by name,
  so a folder you pointed fleet at keeps everything else in it.

## Check it yourself

```bash
fleet paths                   # where fleet keeps its files here
ls ~/.cache/fleet             # on a machine you watch: what the probe keeps
```

## Take it back

On the machine you run fleet on, `fleet uninstall` removes fleet from your agents, and
`--purge` deletes its files too. See [Removing fleet](install.md#removing-fleet).

On the machines you watched there is nothing to take back but the cache and, if you
wrote one, `machine.yaml`.

**Upgrading from 0.5?** fleet 0.5 did write `authorized_keys` and a key on every machine.
[Upgrading from 0.5](upgrading-from-0.5.md) lists what it left and how to remove it.

Found a security problem? Please report it privately; see
[SECURITY.md](https://github.com/sinolion/fleet/blob/main/SECURITY.md).
