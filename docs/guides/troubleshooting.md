# Troubleshooting

Find the message you see. Most problems are a machine that will not let fleet in, and
for those there is one command: `fleet doctor NAME` says why, and what to do.

## Installing

| What you see | What to do |
|---|---|
| `fleet: command not found` after installing | Open a new terminal. If it persists: `uv tool update-shell`, then a new terminal. |
| `No coding agent found` | fleet is installed, but no agent it knows is on this machine yet. Install the agent, then `fleet setup`; or name one with `fleet setup --target claude`. |
| `core: … keeping it` | Not an error: fleet was already installed, and the installer kept that copy. `--force-core` replaces it. |
| `note: --join is no longer needed` (or `FLEET_JOIN`) | A line from fleet 0.5's `fleet invite`. There is nothing to join any more; the installer ignored it and installed fleet as usual. See [Upgrading from 0.5](upgrading-from-0.5.md). |

## Adding machines

| What you see | What to do |
|---|---|
| `NAME refused us — auth_failed` | The machine is up and does not accept you. Nothing was recorded. The lines under it are the fix — usually your key on the machine, your provider's key page, or a Tailscale SSH rule. Then run the same `fleet add` again. [Reaching your machines](reaching-machines.md) has each case. |
| `NAME did not answer — timeout`, `unreachable`, `refused` or `closed` | Nothing was recorded. Check the address, that the machine is on, and that sshd runs there; on a VPN or tailnet, that both ends are connected. |
| `NAME did not answer — host_key_mismatch` | Its host key changed: a rental's port given to someone else, or a reinstall — or someone in the middle. If you know why, `ssh-keygen -R HOST` (on a port other than 22, `ssh-keygen -R '[HOST]:PORT'`; `fleet doctor NAME` prints the line) and add it again. |
| `NAME does not accept your key yet. Put your key on it with …?` | fleet offers `ssh-copy-id`, which asks for the machine's password once and puts your key there. Say yes, or no and fix it another way. |
| `you have no ssh key yet` | Make one: `ssh-keygen -t ed25519`, then `fleet doctor NAME` for the line that puts it on the machine. |
| `it keeps its name; to rename it: fleet edit NAME --name …` | That machine was already known; the new address was added to it under its old name. |
| `named after …; to rename it: …` | fleet picks a name from the address you gave, or else from the machine's own hostname. If it is not the one you want, `fleet edit NAME --name NEW` keeps everything else. |
| `… shares a machine-id with … most likely cloned from the same image` | Two machines made from one image. They are kept apart; nothing to do. |
| `… is not a usable name` | Names and aliases are letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `fleet ls --discover` lists nothing | It reads `Host` entries in `~/.ssh/config` and `tailscale status` here. With neither, add machines by their ssh command. |
| a tailnet machine from `--discover` refuses | It was tried as your local user name. Add it with the right one: `fleet add "ssh ubuntu@NAME"`. |

## Using fleet

| What you see | What it means |
|---|---|
| `auth_failed`, or `host is UP but refused us` | The machine is up and stopped letting fleet in — a key removed, a Tailscale policy changed, a rental recreated without your key. `fleet doctor NAME`. |
| `NAME refused us a moment ago` from `fleet ssh` | The last reading was refused. `fleet doctor NAME` says why; once fixed, `fleet ls NAME -r` tries again. |
| Tailscale SSH wants a browser sign-in | The policy rule for that machine is `check`. An agent can never sign in; make the rule `accept`. |
| `timeout` with an old "last seen" | A machine that keeps not answering is asked less often. Ask it directly: `fleet ls NAME -r`. |
| A machine I added on another computer is missing here | Each machine running fleet keeps its own list. Add it here too, or share `machines.yaml` (`fleet paths`) with your dotfiles. |
| `No machine named 'X'` from `fleet ls X` | The name matches nothing; `fleet ls` lists them all. |
| `No machine named exactly …` | `fleet rm` never acts on a prefix. Give the full name, an alias or the id. |
| `NAME has no endpoint recorded` | fleet knows the machine but not how to reach it. `fleet edit NAME --ssh "ssh user@host"`. |
| `NAME is this machine` from `fleet ssh` | Run the command here, without `fleet ssh`. |
| `claude reads an older description of fleet` (or several agents) | `fleet setup --refresh`. |
| `fleet ls --tag …` finds nothing, and says machines were skipped | Those machines have never been measured, so they have no measured facts yet. `fleet ls -r` measures them; a shared or mobile machine is measured only when named: `fleet ls NAME`. |
| `… is not valid YAML`, or `device X in the inventory is malformed: …` | `machines.yaml` was edited by hand and no longer parses. The message names the file; fix it or restore a copy. fleet changes nothing until it parses. |
| `two machines in the inventory answer to 'NAME'` | Two entries in `machines.yaml` share a name or alias -- often two copies merged in dotfiles. Rename or remove one by hand; until then fleet will not guess which one you meant. |
| `fleet: cannot write PATH (…)` | fleet could not save one of its own files: a read-only folder, a full disk, a folder `sudo` left owned by root. Nothing was changed; free the space or fix the folder's owner and run it again. |
| `cannot write its cache at …; carrying on without saving what this run sees` | The same for the cache. Commands still work and show what they just read; they keep none of it until the folder can be written. |
| `Could not change NAME's machine.yaml: cannot write …/.config/fleet` | That folder, on that machine, is not yours to write. Fix its owner there, or keep tags and cost here with `--local`. |
| `config.yaml: KEY: … is not a number; using N` | A setting has the wrong type. fleet uses the default and says so once. |
| My machines are gone after upgrading from 0.5 | 0.6 starts a new list and does not read 0.5's `inventory.yaml`. Add them again; see [Upgrading from 0.5](upgrading-from-0.5.md). |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | not found, or the operation failed (a machine refused or did not answer, an update failed) |
| 2 | refused or not possible here: bad input, a machine that refused a moment ago, no terminal to confirm |

`fleet ssh NAME -- COMMAND` returns the remote command's own exit code instead, and 255
when ssh itself could not connect. Under `--json`, stdout carries only the JSON document;
a failure is either `{"ok": false, "error": "…"}` there or, with stdout empty, explained
on stderr.

## Still stuck

`fleet COMMAND --help` explains every command, `fleet doctor` looks at every machine
that is failing, and `fleet paths` shows where fleet keeps its files. Please
[open an issue](https://github.com/sinolion/fleet/issues) with what you ran and what it
said.
