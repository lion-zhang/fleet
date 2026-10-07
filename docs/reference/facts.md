# Facts, tags and alerts

What fleet measures about each machine, the words that describe it, and when it warns.
For how to use them, see [Finding the right machine](../guides/find-machines.md).

## Facts

Facts are worked out from a machine's latest reading every time they are shown, and
never stored, so they cannot go stale. `fleet ls --tag WORD` matches facts and
[tags](#tags) alike; repeating `--tag` means *and*.

| Fact | When a machine has it |
|---|---|
| `gpu` | it reports an NVIDIA GPU, or `nvidia-smi` is there but failing, or it is Apple silicon |
| `cuda` | `nvidia-smi` lists at least one GPU |
| `multi-gpu` | `nvidia-smi` lists more than one GPU |
| `metal` | macOS on Apple silicon (no VRAM figure: its memory is shared) |
| `vram-Ng` | its **largest single card** has at least N GiB (see the steps below) |
| `cores-N` | at least N logical CPUs (a 16-core CPU with hyperthreading has 32) |
| `ram-Ng` | at least N GiB of memory |
| `storage-Nt` | its largest writable volume holds at least N TiB in total |
| `linux`, `macos`, `windows` | what it runs |
| `x86_64`, `arm64` | its CPU architecture (`amd64` counts as `x86_64`, `aarch64` as `arm64`) |
| `public-ip` | one of its addresses is a public one |
| `mesh` | it is reached over an overlay network: a `100.64.0.0/10` address, or a name ending in `.ts.net`, `.netbird.cloud` or `.zerotier.net` |
| `lan` | it is reached on a private address |
| `rental`, `shared`, `appliance` | its [kind](#kinds) |

For example `vram-24g`, `ram-64g`, `storage-4t`, `cores-16`.

### Size steps

Size facts mean **at least**, so a machine has every step up to its size: an 80 GiB card
gives `vram-4g` through `vram-80g`. Memory, VRAM and storage get 15% slack, because a
"24 GB" card or "64 GB" machine reports a little less than that: a value counts for a step
when it is at least 85% of it. CPU counts are exact.

| Fact | Steps |
|---|---|
| `vram-Ng` | 4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96, 128, 192 |
| `ram-Ng` | 4, 8, 16, 32, 64, 128, 256, 512, 1024 |
| `cores-N` | 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256 |
| `storage-Nt` | 1, 2, 4, 8, 16, 32, 64 |

A machine that has never been measured has no facts, and matches no `--tag` that is not
one of its tags. `fleet ls --tag` says how many machines it left out for that reason.

## Tags

Tags are your own words, set with `fleet add --tag` or `fleet edit --tag` / `--untag`,
and kept until you change them. They are matched without regard to case. A tag may have
the same name as a fact.

## Kinds

Each machine has a kind. fleet picks it when the machine is added; `fleet add --kind`
overrides that.

| Kind | Picked when | What it changes |
|---|---|---|
| `permanent` | otherwise (the default) | nothing |
| `rental` | the machine shows signs of vast.ai, RunPod or AutoDL, or its address names vast.ai, RunPod, AutoDL, SeetaCloud, Lambda or Paperspace | the `rental` fact, and the idle-rental alert |
| `shared` | it runs SLURM, or more than three people are logged in | measured lightly (no process list; SLURM jobs and partitions instead), only when named (`fleet ls NAME`), never offered for work, and always flagged |
| `appliance` | only with `--kind appliance` | the `appliance` fact; never offered for work |
| `mobile` | only with `--kind mobile` | never measured, never offered for work |

## Alerts

Each machine's alerts are in `alerts` in `fleet ls --json`; the first is shown in the
NOTE column. In order:

| Alert | When |
|---|---|
| *host is UP but rejected our key (this is not 'offline') …* | the machine answered and refused the fleet's key |
| *gpuN: … MiB held by processes not visible to us -- do not treat as free* | a GPU's used memory exceeds what its visible processes account for by 256 MiB or more |
| *disk MOUNT: N% full, NG left -- a long job will die on this* | a writable volume is at least 90% full |
| *nvidia-smi present but failing: …* | `nvidia-smi` is installed but failing |
| *shared multi-user host -- not claimable* | always, for a `shared` machine |
| *RENTAL is idle -- this is costing money* | a `rental` with at least one GPU, none of them busy |

A GPU counts as **busy** at 10% utilisation or more, or with 1 GiB or more of compute
memory in use.

`fleet ls` (not `--json`) can also print a line when the center has not been heard from
for a week, and when an agent on this machine reads an older description of fleet.

## What a reading collects

| | Linux, macOS | Windows |
|---|---|---|
| Host | hostname, OS, kernel, architecture, uptime, logged-in users, container and rental signs | hostname, OS, version, architecture, uptime, logged-in users |
| CPU | logical cores, model, load average | logical cores, model, current load |
| Memory | total and available | total and free |
| GPUs | per card from `nvidia-smi`: name, memory total/used/free, utilisation, temperature, power | the same, when `nvidia-smi` is on the PATH |
| Disks | `/` and mounts under `/workspace`, `/data`, `/mnt`, `/home`, `/srv`, `/opt`, `/Volumes`, `/scratch` (or the paths set with `fleet edit --disk-path`) | fixed drives (or the paths set) |
| Processes | GPU processes, and the busiest CPU processes | — |
| Services | listening TCP ports, named by the program behind them | — |
| SLURM | your jobs and the partitions, on `shared` machines | — |

Everything is gathered by one script sent over one SSH connection, which removes its
temporary file when it finishes.
