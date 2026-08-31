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


def _usage(cmd: str) -> str:
    return f"""## Commands

- `{cmd} ls --json` -- every machine, with what is free right now
- `{cmd} show NAME --json` -- full detail on one machine
- `{cmd} ssh NAME -- COMMAND` -- run a command there

## Rules

- Check `{cmd} ls --json` before assuming work must run locally. A free remote GPU is
  usually the better place for training, evaluation, or anything long-running.
- Connect with `{cmd} ssh NAME`. Never hand-build an `ssh` invocation from inventory
  fields: the wrapper resolves the endpoint so credentials stay out of the transcript.
- Treat `alerts` as blocking. A device reporting unattributed VRAM is not free, and a
  rental flagged idle is costing money right now.
- `status` is not a boolean. `auth_failed` means the host is UP but rejected our key --
  that is a credential problem to report, not an offline machine to skip.
"""


def skill_text(cmd: str) -> str:
    """A Claude Code skill file. fleet owns this file entirely."""
    return (f"---\nname: fleet\ndescription: {_DESCRIPTION}\n---\n\n"
            f"# fleet\n\nYour personal compute inventory.\n\n{_usage(cmd)}")


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
    """Which file each agent reads.

    Claude Code's layout is the same either way. Codex differs: in a repo the convention
    is a top-level AGENTS.md, not a nested dot-directory.
    """
    return {
        "claude": root / ".claude" / "skills" / "fleet" / "SKILL.md",
        "codex": (root / "AGENTS.md") if project else (root / ".codex" / "AGENTS.md"),
    }


def detect_targets(root: Path) -> list[str]:
    """Only agents that are actually installed. Creating ~/.codex for someone who does
    not use Codex would be litter, not setup."""
    return [t for t, d in (("claude", ".claude"), ("codex", ".codex"))
            if (root / d).is_dir()]


def _desired(target: str, path: Path, cmd: str) -> str:
    if target == "claude":
        return skill_text(cmd)              # fleet owns this file outright
    existing = path.read_text() if path.exists() else ""
    return apply_block(existing, agents_block(cmd))


def install(root: Path, targets: list[str], cmd: str, *,
            dry_run: bool = False, project: bool = False) -> list[Change]:
    paths = plan(root, project=project)
    changes: list[Change] = []
    for target in targets:
        path = paths[target]
        current = path.read_text() if path.exists() else None
        desired = _desired(target, path, cmd)
        action = ("unchanged" if current == desired
                  else "created" if current is None else "updated")
        if action != "unchanged" and not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desired)
        changes.append(Change(target, path, action))
    return changes


def uninstall(root: Path, targets: list[str], *,
              dry_run: bool = False, project: bool = False) -> list[Change]:
    paths = plan(root, project=project)
    changes: list[Change] = []
    for target in targets:
        path = paths[target]
        if not path.exists():
            changes.append(Change(target, path, "unchanged"))
            continue
        if target == "claude":
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
