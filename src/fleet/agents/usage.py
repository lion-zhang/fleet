"""What fleet tells an agent about itself.

111 lines of it, and it is prose -- a user manual that happens to be stored as a string.
It sat in the middle of a module that writes files, where every read of that module had
to scroll past it. It is content, and content earns its own file.

Changing the words here changes what every agent on every machine believes fleet can do,
which is worth knowing before editing: `fleet setup` rewrites the installed copies, but
only on machines where someone runs it again.
"""

from __future__ import annotations

from .registry import HERMES_CATEGORY, fleet_command, package_version


_DESCRIPTION = ("Use when the task needs a remote machine or GPU -- lists your compute, "
                "what is free, and connects to it.")



def _usage(cmd: str) -> str:
    """What an agent needs to know, split by what it is allowed to do.

    Deliberately two lists. Most machines can only read the fleet and connect to what
    they were already granted; the center can also change who reaches what. An agent
    that does not know the difference will either not try things that would work, or
    try things that cannot and report a confusing failure as a fault.
    """
    return f"""## Commands

Anywhere. A name may be omitted where the obvious subject is the machine you are on.

- `{cmd} ls --json` -- every machine, with what is free right now
- `{cmd} ls NAME... --json` -- only those, `-r` to force a fresh probe,
  `--online` for reachable ones only
- `{cmd} ls --tag NAME --json` -- **the right way to pick a machine.** Repeatable, and
  all must match: `--tag cuda --tag vram-24g` finds NVIDIA boxes with a card of at
  least 24G. Matches two things at once -- what someone declared about a machine, and
  what the last probe measured. Never pick a machine by remembering its name
- `{cmd} show [NAME] --json` -- full detail; no name means this machine
- `{cmd} ssh NAME -- COMMAND` -- run a command there
- `{cmd} ssh NAME` -- an interactive shell, exactly as plain ssh
- `{cmd} access [NAME]` -- who may reach what, and what is still pending
- `{cmd} center --json` -- `is_center`, who decides, and when it was last heard from
- `{cmd} center --pubkey` -- the key to pre-place on a host that takes no password;
  works with nothing reachable, which is the point
- `{cmd} center --export` -- the access list and pins, worth keeping off the machine
- `{cmd} center --leave` -- take this machine out of the fleet. Needs nobody's
  permission: you own the machine you are on
- `{cmd} top` -- live view; needs a terminal, so not for an agent
- `{cmd} update [NAME]` / `--all` -- deploy the newest fleet from git
- Windows hosts work as targets: they are probed and keyed over PowerShell, and
  `{cmd} ssh box -- cmd` passes the command through rather than wrapping it in a
  POSIX shell. Nothing to configure -- it is read from the last probe
- `{cmd} add "ssh user@host"` -- add a machine to the fleet. It must answer, or it is
  not recorded at all: a machine the center cannot reach cannot be managed. Run on the
  center this also enrols it; run anywhere else it is recorded and the center enrols it
  on the next `{cmd} sync`, and it can be granted nothing until then. `--name` and
  `--kind` override what is guessed, `--alias SHORT` gives it a short handle you can
  type anywhere a name goes, and `--tag NAME` (repeatable) labels it
- `{cmd} add --self` -- record the machine you are on, without ssh
- `{cmd} edit [NAME] --ssh "ssh ..."` -- a rental moved; point the record at the new
  address. `--disk-path /workspace` to watch the volume that matters, `--name` to
  rename, `--alias` to set or clear the short handle, `--tag`/`--untag` to add and
  remove labels (both repeatable)
- `{cmd} install NAME` -- put fleet on a machine that has none
- `{cmd} paths` -- where the inventory, keys and access list live on this machine

Only on the center. `{cmd} center --json` has `is_center`; if it is false,
these either refuse or file a request for the center to act on later.

- `{cmd} access NAME --allow MACHINE` -- grant, and it is applied on the spot.
  `--user` names whose authorized_keys, since a box answers as both root@ and ubuntu@
- `{cmd} access NAME --deny MACHINE` -- revoke
- `{cmd} sync` -- the sweep: enrol anything not yet enrolled, install and remove keys,
  and collect telemetry. Rarely needed now: a grant applies itself, and machines refresh
  from the center on their own. Reach for it to retry something left pending
- `{cmd} sync --from URL` -- run on a machine the center cannot reach, to dial the
  center instead. A machine learns where the center is only when the center reaches it,
  so one behind a firewall, or on a path that fails in that direction, is a full member
  with no way to find it. This is the way in. Ask the user for the address
- `{cmd} center --listen` -- serve the fleet so machines refresh themselves instead of
  waiting to be swept. Long-running: tell the user to run it, do not start it yourself
- `{cmd} center NAME` -- hand the role over; the successor then runs
  `{cmd} center --accept`, which verifies it can write before taking it
- `{cmd} center --init` / `--dissolve` -- create a fleet, or take it down. Dissolving
  removes every key from every machine first; never delete the access list by hand,
  which orphans those keys instead of removing them
- `{cmd} rm NAME` -- remove another machine. Any machine may remove *itself*

## Rules

- Check `{cmd} ls --json` before assuming work must run locally. A free remote GPU is
  usually the better place for training, evaluation, or anything long-running.
- Connect with `{cmd} ssh NAME`. Never hand-build an `ssh` invocation from inventory
  fields: the wrapper resolves the endpoint and uses the fleet key.
- Treat `alerts` as blocking. A device reporting unattributed VRAM is not free, and a
  rental flagged idle is costing money right now.
- `status` is not a boolean. `auth_failed` means the host is UP but rejected our key.
  Report it; do not try to fix it -- only the center can, and it may be offline.
- **Never type a password or accept one from the user.** The only command that asks for
  one is `{cmd} add` on the center, for a host that accepts no key yet, and a human must
  run that themselves. You are not stuck, though: `{cmd} center --pubkey` prints the key
  to put on the host instead, after which enrolment needs no password at all. Say that
  rather than giving up.
- **Ask rather than guess.** These commands need a machine name, sometimes a user,
  sometimes a whole ssh command. If the request does not say, ask -- do not infer a
  machine from a partial name or from whatever was being discussed. `{cmd} rm`,
  `{cmd} access --deny`, `{cmd} center --dissolve` and `{cmd} update --all` are not
  undone by running them again.
- A row in `{cmd} access` that is not `present` is a grant that has not reached its
  target yet, not one that failed. Say so rather than retrying.
- Reading the fleet refreshes it. `{cmd} ls` and `{cmd} show` pull from the center when
  this machine's copy has gone stale, so you do not need `{cmd} sync` to see current
  data -- and a center that is down costs you freshness, never the command.
- The center is expected to be offline -- it is usually a laptop. Everything already
  granted keeps working without it; only *changes* wait. "The center was last seen 3h
  ago" is a normal state to report, not an error.
- A relayed row (`via <machine>` in `{cmd} top`, `source: broadcast` in JSON) was
  measured by the center, not here. Quote its age; do not present it as live.
- **Facts are measured; tags are declared.** `facts` in the JSON is recomputed from the
  last probe, so it is current by construction -- `gpu`, `cuda`, `metal`, `multi-gpu`,
  `vram-NNg`, `linux`/`macos`/`windows`, `x86_64`/`arm64`, `cores-NN`, `ram-NNg`,
  `storage-NNt`, `public-ip`/`mesh`/`lan`, `rental`/`shared`/`appliance`. `tags` is
  whatever a human wrote. `--tag` searches both.
- Size facts mean **at least**: a machine with `vram-48g` also reports `vram-24g`, so
  `--tag vram-24g` is how you ask for "24G or more". `cores-NN` counts logical CPUs,
  so a 16-core part with hyperthreading reports `cores-32`.
- `gpu` without `cuda` means the card is there and the driver is not answering -- do not
  send CUDA work to it. `metal` is an Apple GPU, which reports no VRAM figure.
- A machine with no telemetry has no facts and matches no `--tag`. `{cmd} ls --tag`
  says on stderr how many it could not judge; report that rather than concluding the
  fleet has nothing suitable.
- `{cmd} update --all` touches every machine. Ask first.
"""



def skill_text(cmd: str) -> str:
    """A Claude Code skill file. fleet owns this file entirely."""
    return (f"---\nname: fleet\ndescription: {_DESCRIPTION}\n---\n\n"
            f"# fleet\n\nYour personal compute inventory.\n\n{_usage(cmd)}")



def hermes_skill_text(cmd: str) -> str:
    """A Hermes skill. Same idea as the Claude Code one, different contract.

    Hermes requires name, description, version, author and license -- where Claude Code
    needs only the first two -- and a skill missing any of them is not rejected loudly,
    it simply never loads. license says UNLICENSED because this repo carries no licence
    file, and claiming MIT here would be a claim about someone else's code.
    """
    return (f"---\nname: fleet\ndescription: \"{_DESCRIPTION}\"\n"
            f"version: {package_version()}\nauthor: fleet\nlicense: UNLICENSED\n"
            "platforms: [linux, macos, windows]\n"
            "metadata:\n  hermes:\n"
            "    tags: [fleet, inventory, gpu, ssh, remote, compute]\n"
            "    requires_tools: [terminal]\n"
            f"---\n\n# fleet\n\nYour personal compute inventory.\n\n{_usage(cmd)}")



def agents_block(cmd: str) -> str:
    """The body of the managed region in a shared AGENTS.md."""
    return f"# fleet\n\n{_DESCRIPTION}\n\n{_usage(cmd)}"

