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


_DESCRIPTION = ("Use when the task needs a remote machine or GPU, or the user asks to "
                "install fleet -- lists your compute, what is free, and connects to it.")



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
- `{cmd} center --json` -- `role` (center or member), who decides, and when it was last heard from
- `{cmd} center --pubkey` -- the key to pre-place on a host that takes no password;
  works with nothing reachable, which is the point
- `{cmd} center --export` -- the access list and pins, worth keeping off the machine
- `{cmd} center --leave` -- take this machine out of the fleet. Needs nobody's
  permission: you own the machine you are on
- `{cmd} top` -- live view; needs a terminal, so not for an agent
- `{cmd} update [NAME]` / `--all` -- deploy the newest fleet from git. It also
  rewrites this description on each machine, so what you read here stays current;
  `{cmd} setup --refresh` does just that part
- Windows hosts work as targets: they are probed and keyed over PowerShell, and
  `{cmd} ssh box -- cmd` passes the command through rather than wrapping it in a
  POSIX shell. Nothing to configure -- it is read from the last probe, and the remote
  command's exit code comes back on every OS
- `{cmd} add "ssh user@host"` -- add a machine to the fleet. It must answer, or it is
  not recorded at all: a machine the center cannot reach cannot be managed. Run on the
  center this also enrols it; run anywhere else it is recorded and the center enrols it
  on the next `{cmd} sync`, and it can be granted nothing until then. `--name` and
  `--kind` override what is guessed, `--alias SHORT` gives it a short handle you can
  type anywhere a name goes, and `--tag NAME` (repeatable) labels it. Use the `name`
  in its `--json` reply from then on: a machine already known (one box at a second
  address) keeps its own name, whatever `--name` asked for
- `{cmd} add --self` -- record the machine you are on, without ssh
- `{cmd} join CODE` -- join a fleet with an invite the center issued. The machine dials
  the center, so no password is typed and a machine the center cannot reach can still
  join. `--name` names it if the invite did not, `--ssh "ssh user@addr"` says how the
  center reaches it back when the address it sees is not that (NAT, a jump host)
- `{cmd} edit [NAME] --ssh "ssh ..."` -- a rental moved; point the record at the new
  address. `--disk-path /workspace` to watch the volume that matters, `--name` to
  rename, `--alias` to set or clear the short handle, `--tag`/`--untag` to add and
  remove labels (both repeatable), `--cost 1.89` for what it costs per hour (0 clears)
- `{cmd} install NAME` -- put fleet on a machine that has none
- `{cmd} paths` -- where the inventory, keys and access list live on this machine

Only on the center. `{cmd} center --json` says which this machine is: `role` is
`center` or `member` (empty when it is in no fleet), and `center` names the machine
that decides. These refuse anywhere else and say
where to run them: report that to the user, nothing is queued.

- `{cmd} access NAME --allow MACHINE` -- grant, and it is applied on the spot.
  `--user` names whose authorized_keys, since a box answers as both root@ and ubuntu@
- `{cmd} access NAME --deny MACHINE` -- revoke
- `{cmd} sync` -- on the center, the sweep: enrol anything not yet enrolled, install and
  remove keys, and collect telemetry. On a member, fetch a fresh copy from the center
  now. Rarely needed: a grant applies itself, and machines refresh on their own. Reach
  for it to retry something left pending
- `{cmd} sync --from URL` -- run on a machine the center cannot reach, to dial the
  center instead. A machine learns where the center is only when the center reaches it,
  so one behind a firewall, or on a path that fails in that direction, is a full member
  with no way to find it. This is the way in. Ask the user for the address
- `{cmd} center --listen` -- serve the fleet so machines refresh themselves instead of
  waiting to be swept. Long-running: tell the user to run it, do not start it yourself
- `{cmd} center NAME` -- hand the role over. Grants NAME everything and delivers the
  handover to it (fleet must be installed there); then NAME runs `{cmd} center
  --accept`, which verifies it can write every machine before taking the role. Every
  machine, and the old center, follows by itself. Irreversible: a human runs it.
  Until NAME accepts, the old center refuses changes (they would not carry over);
  `{cmd} center --cancel` there keeps the role if NAME never will
- `{cmd} center --init` / `--dissolve` -- create a fleet, or take it down. Dissolving
  removes every key from every machine first; never delete the access list by hand,
  which orphans those keys instead of removing them
- `{cmd} rm NAME` -- remove another machine: its keys come off every other machine and
  the fleet's keys come off it, at once. Any machine may remove *itself*
- `{cmd} invite [NAME]` -- let one machine join by itself: prints `{cmd} join CODE` to
  run there, and one line that installs fleet and joins in one go on a machine that
  has no fleet yet (`--json`: `command`, `install`, `install_windows`). Single use; `--ttl 30m` for how long (default 15m), `--list` for what
  became of recent ones, `--revoke ID` to withdraw one, `--url` when the machine must
  dial some other address than the one the center listens on. The code admits a
  machine, so hand it to the user and nowhere else: not into a file, a commit or a
  message to anyone. Prefer this to `{cmd} add` when the host takes no key from here

## Rules

- **Right after you install fleet for the user**, say in a few lines what they can do
  next, in their words, not commands: add a machine by giving you the SSH command they
  already use ("add `ssh user@host`"), or ask for an invite line to paste on a machine
  you cannot get into; then ask for what they need ("what's free?", "run this where a
  24 GB card is free") and you will pick the machine. Offer to add the first one.
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
  to put on the host instead, after which enrolment needs no password at all, or
  `{cmd} invite` issues a code the machine joins with by itself. Say that rather than
  giving up.
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
- A machine that keeps not answering is asked less often by a bare `{cmd} ls`: the
  wait doubles per miss, up to half an hour. So `timeout` with an old "last seen" can
  be minutes stale. Before concluding a machine is down -- or when the user says it
  is back -- ask it directly: `{cmd} ls NAME -r --json`
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



def stamp(cmd: str) -> str:
    """Which fleet wrote this text, and which text it is.

    The version alone cannot say: it stayed at 0.4.0 through a run of changes to what
    agents are told, so a copy written weeks apart read as the same. A hash of the words
    does. An HTML comment, so it renders as nothing in every agent that reads markdown.
    """
    import hashlib

    digest = hashlib.sha256((_DESCRIPTION + _usage(cmd)).encode()).hexdigest()[:12]
    return (f"<!-- fleet {package_version()} #{digest} -- written by `fleet setup`; "
            "`fleet setup --refresh` rewrites it -->")


def skill_text(cmd: str) -> str:
    """A Claude Code skill file. fleet owns this file entirely."""
    return (f"---\nname: fleet\ndescription: {_DESCRIPTION}\n---\n\n{stamp(cmd)}\n\n"
            f"# fleet\n\nYour personal compute inventory.\n\n{_usage(cmd)}")



def hermes_skill_text(cmd: str) -> str:
    """A Hermes skill. Same idea as the Claude Code one, different contract.

    Hermes requires name, description, version, author and license -- where Claude Code
    needs only the first two -- and a skill missing any of them is not rejected loudly,
    it simply never loads. The license is the repository's own, MIT.
    """
    return (f"---\nname: fleet\ndescription: \"{_DESCRIPTION}\"\n"
            f"version: {package_version()}\nauthor: fleet\nlicense: MIT\n"
            "platforms: [linux, macos, windows]\n"
            "metadata:\n  hermes:\n"
            "    tags: [fleet, inventory, gpu, ssh, remote, compute]\n"
            "    requires_tools: [terminal]\n"
            f"---\n\n{stamp(cmd)}\n\n# fleet\n\nYour personal compute inventory.\n\n"
            f"{_usage(cmd)}")



def agents_block(cmd: str) -> str:
    """The body of the managed region in a shared AGENTS.md."""
    return f"{stamp(cmd)}\n# fleet\n\n{_DESCRIPTION}\n\n{_usage(cmd)}"




# For the copy `fleet setup` does not write: skills/fleet/SKILL.md, which the Claude Code
# plugin, the Gemini CLI extension and `npx skills add` all ship. It reaches a machine
# before fleet does, so the first thing it must say is what to do when fleet is not
# there. Installing makes the machine the center of a fleet, so never unasked -- but
# when the user asks, the agent installs it, rather than handing the job back to them.
_INSTALL_HINT = """If `fleet` is not found, it is not installed on this machine. Never install it
unasked. When the user asks you to install it, do -- following
https://github.com/lion-zhang/fleet/blob/main/INSTALL.md -- after confirming which of
the two modes this machine is for:

- **center**, the one machine that keeps the fleet and decides who may reach what
  (one that can reach all their devices and is online most of the time), starting a
  new fleet:
  - macOS / Linux: `curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh`
  - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"`
  - or, where piping a script into a shell is not allowed:
    `uv tool install agents-fleet && uv tool update-shell && fleet setup`
- **member** of a fleet that already exists: `fleet invite` on that fleet's center
  prints the one line to run here.

The installer writes outside the project (`~/.local/bin`, agent skill folders, a
background service): if your sandbox blocks that, ask the user to allow it rather
than installing halfway. Afterwards, tell the user to start a new session so this
skill is loaded with fleet present.
"""


def dist_stamp() -> str:
    """Like `stamp`, for the copies that ship in the repository."""
    import hashlib

    digest = hashlib.sha256((_DESCRIPTION + _INSTALL_HINT + _usage("fleet")).encode()
                            ).hexdigest()[:12]
    return (f"<!-- fleet {package_version()} #{digest} -- generated by "
            "scripts/build_dist.py; edit src/fleet/agents/usage.py, not this file -->")


def dist_skill_text() -> str:
    """skills/fleet/SKILL.md: the Claude Code plugin's skill, and `npx skills add`'s."""
    return (f"---\nname: fleet\ndescription: {_DESCRIPTION}\n---\n\n{dist_stamp()}\n\n"
            f"# fleet\n\nYour personal compute inventory.\n\n{_INSTALL_HINT}\n"
            f"{_usage('fleet')}")
