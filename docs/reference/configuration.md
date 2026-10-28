# Configuration

fleet needs no configuration. This page lists what can be changed, and where fleet keeps
its files.

## Files

| OS | Configuration | State |
|---|---|---|
| Linux | `~/.config/fleet` (`$XDG_CONFIG_HOME/fleet`) | `~/.local/state/fleet` (`$XDG_STATE_HOME/fleet`) |
| macOS | `~/Library/Application Support/fleet` | the same folder |
| Windows | `%LOCALAPPDATA%\fleet` | the same folder |

`FLEET_CONFIG_DIR` and `FLEET_STATE_DIR` move them. Both folders are readable by you
alone. `fleet paths` prints where the main files are on this machine.

**Configuration folder**

| File | What it is |
|---|---|
| `machines.yaml` | the machines you added on this machine: names, ssh commands, and the tags and cost you set with `--local`. Written only here; share it with your dotfiles if you want the same list elsewhere. No keys or passwords are in it. |
| `config.yaml` | the settings below. Optional; fleet never creates it. |
| `discover-hidden.txt` | names `fleet ls --discover --hide` stopped offering, one per line |
| `device-id` | only on a machine cloned from another's image: the id fleet gave it, so the two are told apart |

**State folder**

| File | What it is |
|---|---|
| `cache.db` | measurements. Disposable: delete it, and the next reading rebuilds it. |
| `first-run-done` | marks that the [first run](#environment-variables) has happened here |
| `update.log`, `update.ps1`, `update-run.ps1`, `update-run.log` | Windows only: the last `fleet update` of this machine, which runs after fleet exits |

Folders named `.NAME.queue` beside these files are the write queue; they come and go.

Files from fleet 0.5 — `inventory.yaml`, `access*.yaml`, `id_ed25519`, `clock.yaml`,
`center-service.log` and others — are not read by 0.6. They are listed, with how to
remove them, in [Upgrading from 0.5](../guides/upgrading-from-0.5.md#files-in-fleets-folders).

## Settings

`config.yaml` in the configuration folder, for example:

```yaml
telemetry_ttl_s: 30
max_workers: 16
```

| Key | Default | What it changes |
|---|---|---|
| `telemetry_ttl_s` | `60` | Seconds a reading counts as fresh. `ls` and `show` measure a machine again once its reading is older. |
| `offline_backoff_max_s` | `1800` | The longest wait between attempts on a machine that keeps not answering; the wait doubles per miss up to this. |
| `probe_timeout_s` | `20` | Seconds `ls`, `show` and `top` give a machine to answer. |
| `connect_timeout_s` | `8` | ssh connection timeout for those readings. |
| `max_workers` | `8` | How many machines are measured at once. |
| `shared_min_interval_s` | `300` | The shortest time between two measurements of a `shared` machine, so a multi-user cluster is never hammered. |
| `snapshot_retention` | `120` | Readings kept per machine in `cache.db`. |
| `repo` | (empty) | The git URL `fleet install` and `fleet update` deploy from; otherwise the repository fleet was installed from, or GitHub. |

An unreadable `config.yaml` is ignored rather than stopping fleet, with one warning, and
a number setting that is not a number (`no` included) falls back to its default, with one
warning. So does one that is below 1 where 0 cannot work: `max_workers`,
`probe_timeout_s`, `connect_timeout_s` and `snapshot_retention`. A key fleet does not
know is ignored with a warning that names the setting it looks like; 0.5's `sync_ttl_s`
and `access_retry_s` are ignored quietly.

## Environment variables

| Variable | Effect |
|---|---|
| `FLEET_CONFIG_DIR`, `FLEET_STATE_DIR` | where fleet keeps its files |
| `FLEET_NO_FIRST_RUN` | when set, the first `ls`, `show`, `top`, `add` or `setup` on a machine does not record the machine or teach its agents |
| `HERMES_HOME` | where `fleet setup` puts the Hermes skill (default `~/.hermes`, or `%LOCALAPPDATA%\hermes` on Windows) |
| `TERM=dumb` | `fleet top` prints one frame instead of a live view (as it does without a terminal) |

Read by the installers:

| Variable | Installer | Effect |
|---|---|---|
| `FLEET_NO_SETUP` | `install.ps1` | install the command only (`install.sh` takes `--no-setup`) |
| `FLEET_FORCE_CORE` | both | install with uv even though another copy of fleet is here; same as `--force-core` |
| `FLEET_SOURCE` | both | install this package spec — a path, a wheel, a git URL — instead of `agents-fleet` from PyPI |

`FLEET_JOIN` (and `install.sh --join`) is from fleet 0.5: the installers now say it is no
longer needed, and ignore it.

## On the machines you watch

fleet keeps nothing about access there. The probe reads each machine's own
`machine.yaml` — `/etc/fleet/machine.yaml`, then `~/.config/fleet/machine.yaml`
(Windows: `%ProgramData%\fleet\machine.yaml`, then `%LOCALAPPDATA%\fleet\machine.yaml`) —
and on Linux and macOS keeps a cache in `~/.cache/fleet`. The full list is in
[What fleet changes](../guides/what-fleet-changes.md); the file's format is in
[What is on each machine](../guides/resources.md#machineyaml).
