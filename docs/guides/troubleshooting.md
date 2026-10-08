# Troubleshooting

Find the message you see. Most of them are fleet telling you something is *pending*,
not broken.

## Installing

| What you see | What to do |
|---|---|
| `fleet: command not found` after installing | Open a new terminal. If it persists: `uv tool update-shell`, then a new terminal. |
| `service: no service manager for this user here` | A container or a rental. Keep `fleet center --listen` running yourself — tmux, `nohup fleet center --listen &`, or the container's entrypoint. |
| `service: installed, but nothing answers on port 7373 yet` | The service started but is not serving. Its output is in `center-service.log` in fleet's state folder (`fleet paths` shows where). Meanwhile `fleet center --listen` in a terminal does the same job. |
| `No coding agent found` | fleet is installed, but no agent it knows is on this machine yet. Install the agent, then `fleet setup`; or name one with `fleet setup --target claude`. |
| `core: … keeping it` | Not an error: fleet was already installed, and the installer kept that copy. `--force-core` replaces it. |

## Adding machines

| What you see | What to do |
|---|---|
| `NAME did not answer` | Nothing was recorded. Check the address and that sshd runs there, then add it again. |
| `… accepts no key of ours and there is no terminal to type a password` | The machine needs a password, and only you can type it: run `fleet add "ssh …"` yourself in a terminal. Or put the fleet's key there first (`fleet center --pubkey`, run on the center), or use an invite. |
| `… this machine cannot type a password (no pty on Windows)` | A Windows center cannot type passwords. Put the output of `fleet center --pubkey` (run on the center) into the host's `authorized_keys`, or use an invite. |
| `it keeps its name; to rename it: fleet edit NAME --name …` | That machine was already known; the new address was added to it under its old name. |
| `… shares a machine-id with … most likely cloned from the same image` | Two machines made from one image. They are kept apart; nothing to do. |
| `… is not a usable name` | Names and aliases are letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `An invite lasts at most 7 days` | Issue a new one when it is needed; `--ttl 2h` is plenty for most. |
| `this invite has already been used` | Each invite admits one machine. Run `fleet invite` again. |
| `this invite has expired` | Invites last 15 minutes by default. Issue a new one, with `--ttl 2h` if needed. |
| `no answer from …/join` | The center is not listening. Check `fleet center` on it, and that port 7373 is reachable. |
| `HOST did not even resolve here within 5s` | The address in the invite may not work for the new machine either: `fleet invite NAME --url http://ADDRESS:7373/sync`. |

## Using the fleet

| What you see | What it means |
|---|---|
| `This machine is not in a fleet` | Start one with `fleet center --init`, or join one with a code from `fleet invite`. |
| `auth_failed`, or `rejected our key` | The machine is up and refused the key. Only the center can put one back: run `fleet add "ssh …"` for it again on the center (you may have to type its password once). |
| `timeout` with an old "last seen" | A machine that keeps not answering is asked less often. Ask it directly: `fleet ls NAME -r`. |
| A member shows a machine as up that it cannot reach itself | The center can reach it, and the member shows the center's reading. `fleet ls NAME -r` tries again from here. |
| A grant stays `pending`, or a revoke shows `revoking` | The center could not reach that machine when the change was made. The listening center tries again by itself every few minutes (less often while the machine stays off); `fleet sync` on the center tries at once. Until it lands, a revoked key is still there. |
| `the center has not swept this machine for Nd` | Normal for a center that is often off. Everything already granted keeps working; only changes wait. |
| `Only the center can … Run it on NAME` | You are on a member. Run it on the machine named. |
| `the center refused this machine (403 …)` | The center answered and said no: this machine was removed, or its fleet key changed (re-imaged, fleet's files deleted). On the center, invite it again. Not a network problem. |
| `This machine has not heard from its center, NAME, yet` | Reach the center's listener: `fleet sync --from NAME` (or its address), or join with an invite. |
| `This machine is the center; it cannot leave its own fleet` | Hand the role on (`fleet center NAME`) or end the fleet (`fleet center --dissolve`). |
| `No machine named 'X'` from `fleet ls X` | The name matches nothing; `fleet ls` lists them all. |
| `The role is being handed to NAME` | Mid-handover: `fleet center --accept` on NAME, or `fleet center --cancel` here. |
| `No machine named exactly …` | `fleet rm` never acts on a prefix. Give the full name, an alias or the id. |
| `claude reads an older description of fleet` (or several agents) | `fleet setup --refresh`. |
| `fleet ls --tag …` finds nothing, and says machines were skipped | Those machines have never been measured, so they have no measured facts yet. `fleet ls -r` measures them; a shared or mobile machine is measured only when named: `fleet ls NAME`. |
| `… is not valid YAML`, `device X in the inventory is malformed: …`, or `unreadable access list at …` | A state file was edited by hand and no longer parses. The message names the file (`fleet paths` lists them); fix it or restore a copy. fleet changes nothing until it parses. |
| `config.yaml: KEY: … is not a number; using N` | A setting has the wrong type. fleet uses the default and says so once. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | not found, or the operation failed (a machine did not answer, an update failed) |
| 2 | refused or not possible here: wrong machine (not the center), bad input, mid-handover |

`fleet ssh NAME -- COMMAND` returns the remote command's own exit code instead, and 255
when ssh itself could not connect. Under `--json`, stdout carries only the JSON document;
a failure is either `{"ok": false, "error": "…"}` there or, with stdout empty, explained
on stderr.

## Still stuck

`fleet COMMAND --help` explains every command, and `fleet paths` shows where fleet keeps
its files. Please [open an issue](https://github.com/lion-zhang/fleet/issues) with what
you ran and what it said.
