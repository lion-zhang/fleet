# Configuration

fleet needs no configuration. This page lists what can be changed, and where fleet keeps
its files.

## Files

| OS | Configuration and keys | State |
|---|---|---|
| Linux | `~/.config/fleet` (`$XDG_CONFIG_HOME/fleet`) | `~/.local/state/fleet` (`$XDG_STATE_HOME/fleet`) |
| macOS | `~/Library/Application Support/fleet` | the same folder |
| Windows | `%LOCALAPPDATA%\fleet` | the same folder |

`FLEET_CONFIG_DIR` and `FLEET_STATE_DIR` move them. Both folders are readable by you
alone. `fleet paths` prints where the main files are on this machine.

**Configuration folder**

| File | What it is |
|---|---|
| `inventory.yaml` | the machines in the fleet: names, addresses, tags, cost. Shared with every machine. |
| `id_ed25519`, `id_ed25519.pub` | this machine's fleet key. Never regenerate it: other machines know it by this key. |
| `access.yaml` | the access list — who may reach what. On the center only. |
| `config.yaml` | the settings below. Optional; fleet never creates it. |

**State folder**

| File | What it is |
|---|---|
| `cache.db` | measurements. Disposable: delete it, and the next reading rebuilds it. |
| `access-ledger.yaml` | the center's record of which keys are in place and which changes are pending |
| `access-cache.yaml` | on a member: the center's key, its address, and when it was last heard from |
| `access-invites.yaml` | on the center: open invites, stored as hashes |
| `access-chain.yaml`, `access-handover-*.yaml` | handovers of the center role |
| `center-service.log` | macOS only: the background service's output |

Folders named `.NAME.queue` beside these files are the write queue; they come and go.

**Do not delete `access.yaml` to end a fleet.** It would leave every key in place with
nothing able to remove them. Use `fleet center --dissolve`.

## Settings

`config.yaml` in the configuration folder, for example:

```yaml
telemetry_ttl_s: 30
max_workers: 16
```

| Key | Default | What it changes |
|---|---|---|
| `telemetry_ttl_s` | `60` | Seconds a reading counts as fresh. `ls` and `show` measure a machine again once its reading is older. |
| `offline_backoff_max_s` | `1800` | The longest wait between attempts on a machine that keeps not answering; the wait doubles per miss up to this. Also caps how long a member waits between attempts to reach a silent center. |
| `sync_ttl_s` | `300` | On a member, how old the last contact with the center may be before `ls` or `show` asks it for fresh information. |
| `probe_timeout_s` | `20` | Seconds `ls`, `show` and `top` give a machine to answer. |
| `connect_timeout_s` | `8` | ssh connection timeout for those readings. |
| `max_workers` | `8` | How many machines are measured, or updated, at once. |
| `snapshot_retention` | `120` | Readings kept per machine in `cache.db`. |
| `repo` | (empty) | The git URL `fleet install` and `fleet update` deploy from; otherwise the repository fleet was installed from, or GitHub. |

An unreadable `config.yaml` is ignored rather than stopping fleet. The center's port,
7373, is not a setting; `fleet center --listen --port N` uses another one for that run.

## Environment variables

| Variable | Effect |
|---|---|
| `FLEET_CONFIG_DIR`, `FLEET_STATE_DIR` | where fleet keeps its files |
| `FLEET_NO_AUTO_CENTER` | when set, a machine in no fleet is not made a center by its first fleet command |
| `HERMES_HOME` | where `fleet setup` puts the Hermes skill (default `~/.hermes`, or `%LOCALAPPDATA%\hermes` on Windows) |
| `TERM=dumb` | `fleet top` prints one frame instead of a live view (as it does without a terminal) |

Read by the installers:

| Variable | Installer | Effect |
|---|---|---|
| `FLEET_JOIN` | both | join this fleet as a member; same as `--join CODE` |
| `FLEET_NO_SETUP` | `install.ps1` | install the command only (`install.sh` takes `--no-setup`) |
| `FLEET_FORCE_CORE` | both | install with uv even though another copy of fleet is here; same as `--force-core` |
| `FLEET_SOURCE` | both | install this package spec — a path, a wheel, a git URL — instead of `agents-fleet` from PyPI |

## The background service

| OS | What is installed | Runs |
|---|---|---|
| Linux | `~/.config/systemd/user/fleet-center.service`, with lingering enabled | `fleet center --listen --port 7373` |
| macOS | `~/Library/LaunchAgents/io.fleet.center.plist` | the same |
| Windows | scheduled task `fleet-center` at logon, and firewall rules named `fleet center` | the same |

It runs as you, never as root, and serves on all interfaces. See
[The center](../guides/center.md#the-background-service).

## On the machines you add

fleet writes only to `authorized_keys` there — `~/.ssh/authorized_keys`, or on Windows
for administrators `C:\ProgramData\ssh\administrators_authorized_keys` — and only inside
blocks it marks with its fleet id:

```
# fleet:7f3a9c:begin from=SHA256:... user=root
ssh-ed25519 AAAA... fleet:7f3a9c:laptop
# fleet:7f3a9c:end from=SHA256:...
```

Every other line — your keys, a provider's, another fleet's — is left alone. Measuring a
machine leaves nothing behind but a temporary file, removed when it finishes.
