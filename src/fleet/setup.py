"""Install fleet's usage instructions into coding agents.

This is the only module that writes outside fleet's own directories, so it is built
around one rule: never touch a byte we did not write. Files we fully own (a skill file)
are overwritten wholesale; files that belong to the user (AGENTS.md) get a
marker-delimited region and nothing else is disturbed.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

BEGIN = "<!-- BEGIN fleet (managed by `fleet setup`) -->"
END = "<!-- END fleet -->"

_REGION = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)

_DESCRIPTION = ("Use when the task needs a remote machine or GPU -- lists your compute, "
                "what is free, and connects to it.")


def fleet_command() -> str:
    """The command an agent should actually type.

    A skill that says `fleet ls` is worthless if fleet is not on PATH, so fall back to
    an absolute path. The subtle case is a venv: while one is active its bin directory
    sorts first, so a plain `which` resolves to a copy the agent's shell -- which does
    not inherit that activation -- cannot see. Skip venv directories and keep walking:
    a real install further down PATH still means the bare name works everywhere.
    """
    prefix = Path(sys.prefix).resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        try:
            if directory.resolve().is_relative_to(prefix):
                continue
        except OSError:                     # unreadable PATH entry; not our problem
            continue
        exe = directory / "fleet"
        if exe.is_file() and os.access(exe, os.X_OK):
            return "fleet"
    return str(Path(sys.executable).resolve().parent / "fleet")


# Hermes organises skills into categories; the docs' own example for infrastructure
# tooling is skills/devops/<name>/. fleet is inventory and remote execution, so devops.
HERMES_CATEGORY = "devops"


@dataclass(frozen=True, slots=True)
class Agent:
    """One coding agent, as data rather than a branch.

    Supporting a new one is an entry in AGENTS below. Only four things actually vary:
    where its file lives in a home directory, where it lives inside a repo, whether
    fleet owns that file outright or must merge into one the user owns, and how to tell
    the agent is installed at all. Everything else -- the text, the marker region, the
    dedupe, the uninstall -- is already shared.

    Ownership is read from the filename rather than declared: a SKILL.md is ours to
    write wholesale, anything else is the user's and gets a marked region. That rule
    predates this table and is the one thing that must never be got wrong, so it stays
    in one place.
    """

    name: str
    home: str                              # path under the home root, "/"-separated
    project: str                           # path under a repo root
    skill: str = "std"                     # frontmatter dialect when the file is a SKILL.md
    detect: str = ""                       # directory meaning "installed"; default .<name>
    legacy: tuple[str, ...] = ()           # paths we used to write and must now clean up

    @property
    def marker(self) -> str:
        return self.detect or f".{self.name}"


AGENTS = (
    Agent("claude", home=".claude/skills/fleet/SKILL.md",
          project=".claude/skills/fleet/SKILL.md"),
    # A skill, not ~/.codex/AGENTS.md. Codex grew a skills directory -- ~/.codex/skills,
    # same frontmatter as Claude Code's -- and AGENTS.md is read into every conversation
    # whether or not it is about machines. That is the reasoning the hermes entry below
    # already applies to SOUL.md; it holds here for the same reason. The old file is
    # listed as legacy so the block we left in it is taken back out.
    Agent("codex", home=".codex/skills/fleet/SKILL.md", project="AGENTS.md",
          legacy=(".codex/AGENTS.md",)),
    # Not SOUL.md: that is Hermes's system prompt, so a block there would cost tokens in
    # every conversation. Skills load only when a task needs them.
    Agent("hermes", home=f".hermes/skills/{HERMES_CATEGORY}/fleet/SKILL.md",
          project="AGENTS.md", skill="hermes"),
    # GEMINI.md belongs to the user, so it gets a marked region like AGENTS.md rather
    # than being written wholesale.
    Agent("gemini", home=".gemini/GEMINI.md", project="GEMINI.md"),
)

# The one list. cli.py validates against this rather than repeating it.
TARGETS = tuple(a.name for a in AGENTS)
BY_NAME = {a.name: a for a in AGENTS}


def package_version() -> str:
    """The installed version, never a hardcoded one, which would drift immediately."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("fleet-broker")
    except PackageNotFoundError:            # running from a source tree, not installed
        return "0.0.0"


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

- `{cmd} access NAME --allow MACHINE` -- grant, then `{cmd} sync` to apply it.
  `--user` names whose authorized_keys, since a box answers as both root@ and ubuntu@
- `{cmd} access NAME --deny MACHINE` -- revoke
- `{cmd} sync` -- the sweep: enrol anything not yet enrolled, install and remove keys,
  and collect telemetry
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


def apply_block(existing: str, body: str) -> str:
    """Insert or update our region, leaving every other byte of the file alone."""
    block = f"{BEGIN}\n{body}\n{END}"
    if _REGION.search(existing):
        return _REGION.sub(lambda _: block, existing, count=1)
    prefix = existing.rstrip("\n")
    return (prefix + "\n\n" if prefix else "") + block + "\n"


def remove_block(existing: str) -> str:
    """Drop our region. A file we never marked comes back untouched."""
    if not _REGION.search(existing):
        return existing
    return _REGION.sub("", existing).rstrip("\n") + "\n"


@dataclass(slots=True)
class Change:
    """One file setup would touch. `action` is what happened, or would happen."""

    target: str
    path: Path
    action: str                            # created | updated | unchanged | removed


def plan(root: Path, *, project: bool = False) -> dict[str, Path]:
    """Which file each agent reads. Straight off the table.

    Several agents share AGENTS.md inside a repo -- that is the cross-vendor convention,
    and it is why install() dedupes by path rather than by agent.
    """
    return {a.name: root.joinpath(*(a.project if project else a.home).split("/"))
            for a in AGENTS}


def legacy_paths(root: Path) -> dict[str, list[Path]]:
    """Files an agent used to read, which we may still have a region in.

    Moving where we write is not finished until the old copy is gone: a stale block in
    ~/.codex/AGENTS.md would keep being loaded into every conversation, which is the
    cost the move exists to avoid, and it would document a command surface that drifts.
    """
    return {a.name: [root.joinpath(*p.split("/")) for p in a.legacy] for a in AGENTS}


def detect_targets(root: Path) -> list[str]:
    """Only agents that are actually installed. Creating ~/.codex for someone who does
    not use Codex would be litter, not setup."""
    return [a.name for a in AGENTS if (root / a.marker).is_dir()]


def _desired(target: str, path: Path, cmd: str) -> str:
    # A SKILL.md is a file fleet owns outright; anything else belongs to the user and
    # gets a marked region. Read from the filename rather than declared per agent,
    # because it is the one rule that must never be got wrong.
    if path.name == "SKILL.md":
        agent = BY_NAME.get(target)
        return (hermes_skill_text(cmd) if agent and agent.skill == "hermes"
                else skill_text(cmd))
    existing = path.read_text() if path.exists() else ""
    return apply_block(existing, agents_block(cmd))


def install(root: Path, targets: list[str], cmd: str, *,
            dry_run: bool = False, project: bool = False) -> list[Change]:
    paths = plan(root, project=project)
    changes: list[Change] = []
    seen: set[Path] = set()
    for target in targets:
        path = paths[target]
        # Codex and Hermes both read ./AGENTS.md in a project. The block is idempotent
        # so writing twice is harmless, but reporting two changes for one file is a lie
        # about what happened.
        if path in seen:
            continue
        seen.add(path)
        current = path.read_text() if path.exists() else None
        desired = _desired(target, path, cmd)
        action = ("unchanged" if current == desired
                  else "created" if current is None else "updated")
        if action != "unchanged" and not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desired)
        changes.append(Change(target, path, action))
        changes += _drop_legacy(root, target, seen, dry_run=dry_run)
    return changes


def _drop_legacy(root: Path, target: str, seen: set[Path], *, dry_run: bool) -> list[Change]:
    """Take our region back out of anywhere this agent used to read.

    Only ever removes a marked region, never a file: these are the user's files, and the
    rule that we touch no byte we did not write does not stop applying because we have
    changed our minds about where to write. A file we never marked comes back identical,
    so this is silent in the ordinary case.
    """
    out: list[Change] = []
    for path in legacy_paths(root).get(target, []):
        if path in seen or not path.exists():
            continue
        current = path.read_text()
        stripped = remove_block(current)
        if stripped == current:
            continue
        seen.add(path)
        if not dry_run:
            # Nothing but our region was ever in it -- that file existed because fleet
            # made it -- so leaving an empty one behind is litter, not caution.
            if stripped.strip():
                path.write_text(stripped)
            else:
                path.unlink()
        out.append(Change(target, path, "removed"))
    return out


def uninstall(root: Path, targets: list[str], *,
              dry_run: bool = False, project: bool = False) -> list[Change]:
    paths = plan(root, project=project)
    changes: list[Change] = []
    seen: set[Path] = set()
    for target in targets:
        path = paths[target]
        # Codex and Hermes both read ./AGENTS.md in a project. The block is idempotent
        # so writing twice is harmless, but reporting two changes for one file is a lie
        # about what happened.
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            changes.append(Change(target, path, "unchanged"))
            continue
        # Ownership is read from the filename, exactly as _desired reads it. Dispatching
        # on the agent's name instead meant uninstalling Hermes left its skill file on
        # disk -- it writes a SKILL.md too, and only Claude Code was named here.
        if path.name == "SKILL.md":
            if not dry_run:
                path.unlink()
                # our own directory, safe to drop once empty; never touch skills/ itself
                with suppress(OSError):
                    path.parent.rmdir()
            changes.append(Change(target, path, "removed"))
            continue
        current = path.read_text()
        stripped = remove_block(current)
        action = "removed" if stripped != current else "unchanged"
        if action == "removed" and not dry_run:
            path.write_text(stripped)
        changes.append(Change(target, path, action))
    return changes
