> **The secrets half of this document is superseded by [access.md](access.md).**
> `secrets.age`, age recipients and `fleet identity` no longer exist: the center
> installs an SSH key once and there is no credential left to keep. The sync half
> below is still accurate.

# Sync and secrets

Status: design, 2026-08-31. Implements the `role: center | backup` field that has been
declared on `Device` since the first commit but used nowhere, and the `secrets.age` /
`pyrage` extra that `.gitignore` and `pyproject.toml` already reserve.

## The problem

fleet's state is per-machine. Add a rental on the laptop and the desktop has never heard
of it. `inventory.py` anticipated this from the start:

> you will edit, diff, and eventually sync. It must remain readable when the tool is
> [unavailable]

Separately, hosts that only accept password auth cannot be used at all. `fleet key
install` fixes the common case by bootstrapping key auth from a password typed once, but
a host that genuinely refuses key auth still needs a stored credential.

The two are one design, because how a secret is encrypted decides whether it can sync.

## What syncs

| File | Syncs | Why |
| --- | --- | --- |
| `inventory.yaml` | yes | Durable identity. The thing you actually curate. |
| `secrets.age` | yes, encrypted | Useless on one machine only. |
| `cache.db` | **no** | `store.py`: *"disposable by design: delete it and the next probe rebuilds everything."* Syncing telemetry would mean shipping stale readings that the receiving machine could produce itself, more accurately, in one probe. |
| `identity.age` | **never** | The private half. Leaving one machine is the whole failure mode. |

## Transport: a center node over SSH

One device carries `role: "center"`. Every other machine syncs to it over the SSH
connections fleet already manages -- no third-party service, no account, and it works on
a tailnet with no internet.

```
fleet sync
  -> ssh center 'fleet sync --serve'     # canonical state, under a lock
  <- merged inventory
  -> push merged result back
```

The center holds the canonical copy. `--serve` runs on the far side and speaks a small
line protocol over stdin/stdout, reusing `build_argv` exactly as the probe does. It takes
the same `filelock` the local writer takes, so two machines syncing at once serialise
rather than interleave.

**Cost of this choice:** the center being down blocks syncing. That is acceptable because
sync is not on the critical path -- every command works from local state, and a failed
sync is a warning, not an error. `role: "backup"` is set by `fleet install`, which is the
one place fleet leaves code on a machine -- see below.

## Merge

Per device, keyed by `id` -- which is why `id` prefers machine-id over an address, and
why `fleet edit` migrates a `net:` id rather than orphaning it.

1. Present on one side only -> keep it.
2. Present on both -> the record with the newer `updated_at` wins whole. Not
   field-wise: merging fields independently can assemble a device that never
   existed on either machine, and there is no timestamp per field to justify it.
3. Endpoints -> union, deduped on `(target, user, port)`. A box reachable on the tailnet
   from one machine and the LAN from another is one box with two routes, which is the
   dedupe `inventory.upsert` already performs at onboarding.

This requires a new `updated_at` field on `Device`, stamped by every mutating command.
`added_at` cannot serve: it records when the device was first seen, not when its record
last changed.

**Deletion is a tombstone, not an absence.** A record that merely vanishes is
indistinguishable from one the other machine has not seen yet, so the next sync would
resurrect it. `fleet rm` therefore sets `deleted_at` and keeps the record; every
user-facing read goes through `inventory.live()`, and the existing newer-wins rule
carries the deletion without needing any special case -- re-adding a device later simply
produces a newer record, so it comes back rather than being permanently poisoned.
Tombstones older than 30 days are pruned on save, by which point every machine has
certainly seen them.

## Secrets: per-machine age identities

Each machine generates its own age keypair on first use:

```
~/.config/fleet/identity.age     private, mode 0600, never syncs, never printed
```

Its public recipient is published onto that machine's own `Device` record, as a
`recipient:` field. A recipient belongs to a machine, machines are already devices, and
devices already sync and merge -- so this needs no schema, no separate file, and no
special case in the merge:

```yaml
devices:
  - name: machine_B
    recipient: age1ql3z7hjy...
```

`secrets.age` is encrypted to **every** recipient, so any enrolled machine decrypts it
and no shared passphrase ever travels. Enrolling a machine adds a recipient and
re-encrypts; revoking one removes the recipient and re-encrypts, which is a real
revocation rather than a hope.

### Why not the alternatives

- **One passphrase** -- has to reach each new machine somehow, gets typed constantly, and
  one leak compromises every stored password with no way to revoke a single machine.
- **macOS Keychain** -- does not exist on the Linux boxes this fleet is mostly made of.

### The boundary does not move

`connect_view` keeps returning `secret_ref: "fleet://secret/<name>"` and never a value.
Storing a password changes where the secret lives, not who may see it: a decrypted
password is passed to `ssh` through a pty, exactly as `keys.py` already does, and never
enters a view, a log, an argv, or an agent transcript.

## Installing on a device

`fleet install NAME` promotes a device from probe target to infrastructure. It is the
single exception to the README's "targets need nothing installed": a backup node has to
run fleet, so fleet has to be there.

The device clones the repo and `uv tool install`s it. uv is fetched when missing, which
also solves the interpreter problem -- fleet needs Python 3.12 and most servers ship
older, and uv fetches its own rather than requiring one. Re-running updates in place, so
`fleet install` doubles as `fleet update`.

Authentication uses a forwarded SSH agent by default, so the device authenticates to
GitHub as you and **no credential is left on it**. Forwarding does let root on that box
use your agent while you are connected, so `--no-forward-agent` exists for hosts not
trusted with that; those need a deploy key instead.

A failed install does not record the role. `fleet ls` claiming a backup that does not
exist is worse than showing none, because that is the claim you would rely on at exactly
the moment the center is down.

## Sync happens on its own

Nobody should have to remember to run `fleet sync`.

**On machines you use**, any command fires a sync in the background when the last one has
gone stale (`sync_ttl_s`, default 300s). This mirrors telemetry, where a read already
refreshes what is stale rather than making you ask. It is spawned detached and never
waited on: sync is a convenience, and `fleet ls` must keep working with a dead center, an
unreadable config, or no network. Every failure in that path is swallowed for that
reason, and the timestamp is stamped *before* spawning so a hanging sync cannot make
every subsequent command spawn another.

**On brokers**, that is not enough -- a backup node may go weeks without anyone running a
command, and a replica that quietly stopped replicating is worse than no replica, because
you would still be counting on it. So `fleet install` also writes a cron entry. cron
rather than a systemd user unit: it is on far more servers, and needs no lingering
enabled to run without a login session. The entry is marked with a comment so
re-installing replaces our line and leaves every other entry alone.

## Phases

1. `updated_at` + the merge function. Pure, testable without any transport.
2. `fleet sync` / `--serve` over SSH, with locking on the center.
3. `fleet identity` + recipients on each machine's own device record.
4. `fleet secret set/ls/rm`, and spending a stored password:
   - the **probe** retries an auth_failed host with its stored password, as a serial
     fallback after the sweep rather than inside the fan-out. Its stdin is the pty
     carrying the prompt, so the payload rides in argv base64-encoded, and output is
     sliced from the first section marker because a pty merges stderr in;
   - **`fleet ssh`** uses SSH_ASKPASS, which is the only way to answer the prompt
     without standing between the user and their shell. The helper reads the password
     from the environment; writing it into a file would outlive the connection and
     defeat encrypting it at rest.

Phases 1-2 are useful with no secrets at all. Phases 3-4 are useless without 1-2. The
order is forced.
