# Finding the right machine

You describe the work; the agent finds where it fits. This page explains what it goes
by, so you can ask well — and check its choice.

> **You:** train `train.py` on whatever has a free 24 GB card
>
> **Agent:** `rtx4090` has 23.1 GB free and an idle GPU; `a100-spot` is free too but costs
> $1.89/hr. Starting on `rtx4090`, logging to `train.log`.

## What is free right now

`fleet ls` lists every machine with what is free on it at this moment: GPUs and their
free VRAM, CPU cores, free memory, free disk, its hourly price, and a note when something
needs attention.

```bash
fleet ls                    # every machine
fleet ls gpu-box -r         # one machine, measured again now
fleet ls --online           # only the ones that answer
fleet top                   # the same, live, in a terminal (q quits)
fleet show gpu-box          # one machine in full: GPU processes, top CPU users, services, disks
```

A reading is reused for 60 seconds, so asking twice in a row is instant. A machine that
keeps not answering is asked less often — the wait doubles per miss, up to 30 minutes —
so one switched-off box never makes `fleet ls` slow. Naming it (`fleet ls NAME`) always
asks it directly.

**A busy GPU is not a free one.** A card counts as busy at 10% utilisation or 1 GiB of
compute memory in use. VRAM held by processes fleet cannot see — another container,
another user — is flagged rather than reported as free.

## By what a machine has: facts

Every machine carries **facts**, measured by fleet on each reading. You never set them,
and they cannot go stale: take a GPU out, and its facts go with it.

| Facts | Meaning |
|---|---|
| `gpu`, `cuda`, `metal`, `multi-gpu` | it has a GPU; NVIDIA with a working driver; Apple silicon; more than one card |
| `vram-24g`, `vram-48g`, … | its largest card has at least that much memory |
| `cores-16`, `ram-64g`, `storage-4t` | at least that many logical CPUs, that much memory, that much room on its largest volume |
| `linux`, `macos`, `windows`, `x86_64`, `arm64` | what it runs |
| `public-ip`, `mesh`, `lan` | how it is reached: a public address, an overlay such as Tailscale, a local network |
| `rental`, `shared`, `appliance` | what kind of machine it is |

Size facts mean **at least**, so `vram-24g` matches a 24 GB card and an 80 GB one. The
exact steps are in the [reference](../reference/facts.md).

```bash
fleet ls --tag cuda                      # every NVIDIA machine
fleet ls --tag cuda --tag vram-24g       # ...with a card of at least 24 GB
fleet ls --tag linux --tag ram-64g       # repeats mean "and"
```

`gpu` without `cuda` means the card is there but its driver is not answering — often
after a kernel upgrade — so the machine is kept away from CUDA work. A machine fleet has
never measured has no facts and matches nothing; `fleet ls --tag` says how many it had
to leave out.

## Disks: tell fleet where the space is

fleet watches `/` and any mount under `/workspace`, `/data`, `/mnt`, `/home`, `/srv`,
`/opt`, `/Volumes` or `/scratch` (on Windows: every fixed drive). That covers most
machines, but not all of them, and a wrong disk misleads the agent twice: the free
space it reports, and the `storage-Nt` fact it picks machines by.

- **Containers and GPU rentals** often show `/` as a small overlay, while the real space
  is a volume somewhere else.
- **Some systems do not report `/` at its real size** — the data lives on a volume
  mounted under a name fleet does not look for.
- **A quota or a big directory** matters more than the filesystem around it.

Name the paths to watch, and fleet watches **exactly those** from the next reading on:

> **You:** on gpu-box, watch /workspace and /data/datasets for disk space

```bash
fleet edit gpu-box --disk-path /workspace --disk-path /data/datasets
fleet edit gpu-box --disk-path / --disk-path /workspace   # keep / as well
fleet edit gpu-box --clear-disk-paths                     # back to detecting them
```

A path can be a mount point or any directory; fleet measures the disk that holds it and
labels it with the path you gave. A path that does not exist on the machine is skipped.
`fleet show gpu-box` lists the disks as fleet now sees them, and the 90%-full alert and
the `storage-Nt` fact follow the paths you set.

## By what you call it: tags

Tags are labels of your own, for what no measurement can tell: `prod`, `nas`, `quiet`,
`team-a`. They stay until you change them.

> **You:** tag the NAS as backup

```bash
fleet edit nas --tag backup --tag quiet
fleet edit nas --untag quiet
fleet ls --tag backup
```

`--tag` searches tags and facts alike. A tag may have a fact's name: if a machine has an
accelerator fleet cannot see, tagging it `gpu` yourself is the right move, and nothing
will overwrite it.

## By what it costs

Tell fleet what a paid machine costs per hour, and the agent can weigh it:

```bash
fleet edit a100 --cost 1.89
```

The price shows in the `$/HR` column, and `fleet ls --json` adds up the hourly burn of
everything online. A rental whose GPUs are all idle is flagged *RENTAL is idle -- this is
costing money*, whatever its price.

## Alerts

`fleet ls` puts the first alert for each machine in its NOTE column; the agent reads all
of them, and treats them as blocking:

- the machine is up but **refused the fleet's key** — this is not "offline";
- **VRAM held by processes fleet cannot see** — do not treat it as free;
- a **disk at 90%** or more;
- **`nvidia-smi` failing**;
- a **shared multi-user host** — not for claiming;
- an **idle rental**.

The full conditions are in the [reference](../reference/facts.md#alerts).

## Machines that are measured differently

- **Shared hosts** — a cluster login node, or a box with more than three people logged in
  — are recognised by themselves. They are measured lightly and only when you name them
  (`fleet ls NAME`), and never offered for claiming.
- **Appliances** and **mobile** devices (`fleet add … --kind appliance`) are listed but
  never offered for work; mobile ones are not measured at all.
- **Windows** machines report no per-process detail or listening services.
