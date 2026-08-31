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
sync is a warning, not an error. `role: "backup"` is reserved for a promotable second
node and is out of scope here.

## Merge

Per device, keyed by `id` -- which is why `id` prefers machine-id over an address, and
why `fleet edit` migrates a `net:` id rather than orphaning it.

1. Present on one side only -> keep it.
2. Present on both -> field-wise, newer `updated_at` wins.
3. Endpoints -> union, deduped on `(target, user, port)`. A box reachable on the tailnet
   from one machine and the LAN from another is one box with two routes, which is the
   dedupe `inventory.upsert` already performs at onboarding.

This requires a new `updated_at` field on `Device`, stamped by every mutating command.
`added_at` cannot serve: it records when the device was first seen, not when its record
last changed.

**Deletion is not synced in v1.** Distinguishing "deleted on the laptop" from "not yet
seen by the laptop" needs tombstones, and getting that wrong silently resurrects or
silently destroys devices. `fleet rm` therefore removes locally, and removing everywhere
means running it on the center. This is a known, documented gap rather than an accident.

## Secrets: per-machine age identities

Each machine generates its own age keypair on first use:

```
~/.config/fleet/identity.age     private, mode 0600, never syncs, never printed
```

Its public recipient is published into `inventory.yaml` under a top-level `recipients:`
map, so it syncs like everything else:

```yaml
recipients:
  lin-xps:  age1ql3z7hjy...
  macbook:  age1lggyhqrq...
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

## Phases

1. `updated_at` + the merge function. Pure, testable without any transport.
2. `fleet sync` / `--serve` over SSH, with locking on the center.
3. `fleet identity` + recipients in inventory.
4. `fleet secret set/rm` and password use at connect time.

Phases 1-2 are useful with no secrets at all. Phases 3-4 are useless without 1-2. The
order is forced.
