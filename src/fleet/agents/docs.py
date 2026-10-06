"""Writing the instructions into files fleet does not own.

**This is the half that writes outside fleet's own directories**, and it is built around
one rule: never touch a byte we did not write. Files we fully own (a skill file) are
overwritten wholesale; files that belong to the user (AGENTS.md) get a marker-delimited
region and nothing else in them is disturbed.

Running it twice must not differ from running it once. A second run that duplicates a
block, or reports "changed" when nothing did, is the worst failure available to a setup
command -- the user stops trusting its output and starts checking the files by hand.
"""

from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path

from ..state.writes import replace_file
from .registry import AGENTS, BY_NAME, TARGETS, Change
from .usage import agents_block, hermes_skill_text, skill_text


BEGIN = "<!-- BEGIN fleet (managed by `fleet setup`) -->"



END = "<!-- END fleet -->"



_REGION = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)



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



def plan(root: Path, *, project: bool = False, platform: str = "") -> dict[str, Path]:
    """Which file each agent reads. Straight off the table.

    Several agents share AGENTS.md inside a repo -- that is the cross-vendor convention,
    and it is why install() dedupes by path rather than by agent.
    """
    return {a.name: (root.joinpath(*a.project.split("/")) if project
                     else a.home_path(root, platform))
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
    return [a.name for a in AGENTS
            if (root / a.marker).is_dir() or a.home_dir(root).is_dir()]



def installed_targets(root: Path, *, project: bool = False) -> list[str]:
    """Agents fleet has already taught here: a skill file it owns, or a marked region.

    What `fleet setup --refresh` rewrites. Deliberately not `detect_targets`: an agent
    being installed is not the user asking for fleet to be in it, and a refresh that ran
    on every update must never add fleet to an agent somebody chose to leave alone.
    """
    out = []
    for target, path in plan(root, project=project).items():
        try:
            text = path.read_text()
        except OSError:
            text = None
        if text is not None and (path.name == "SKILL.md" or BEGIN in text):
            out.append(target)
        elif not project and _left_behind(root, target):
            # Taught at an older location -- ~/.codex/skills, a region in GEMINI.md. Still
            # "set up", so a refresh moves it to where the agent reads it now, rather than
            # leaving the old copy for ever.
            out.append(target)
    return out


def _left_behind(root: Path, target: str) -> bool:
    agent = BY_NAME.get(target)
    if agent is None:
        return False
    for path in agent.stray_paths(root):
        try:
            if path.is_file() and "name: fleet" in path.read_text().split("---", 2)[1]:
                return True
        except (OSError, IndexError):
            continue
    for path in legacy_paths(root).get(target, []):
        try:
            if BEGIN in path.read_text():
                return True
        except OSError:
            continue
    return False


def stale_targets(root: Path, cmd: str, *, project: bool = False) -> list[str]:
    """Installed agents whose copy differs from what this fleet would write now."""
    paths = plan(root, project=project)
    out = []
    for target in installed_targets(root, project=project):
        path = paths[target]
        try:
            if path.read_text() != _desired(target, path, cmd):
                out.append(target)
        except OSError:
            continue
    return out


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
            # Written already for another agent that reads the same file -- but this
            # agent's own old copies still have to go.
            changes += _drop_legacy(root, target, seen, dry_run=dry_run)
            continue
        seen.add(path)
        current = path.read_text() if path.exists() else None
        desired = _desired(target, path, cmd)
        action = ("unchanged" if current == desired
                  else "created" if current is None else "updated")
        if action != "unchanged" and not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            replace_file(path, desired)
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
    agent = BY_NAME.get(target)
    for path in agent.stray_paths(root) if agent else []:
        # A copy of our own skill where this agent no longer looks. Ours outright -- it
        # is a SKILL.md naming fleet -- so it goes, file and our directory; left, it is
        # the one the agent cannot see, and a second fleet if it ever looks there too.
        if path in seen or not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        front = text.split("---", 2)[1] if text.startswith("---") else ""
        if "name: fleet" not in front:
            continue                       # not a skill fleet wrote; not ours to remove
        seen.add(path)
        if not dry_run:
            path.unlink()
            with suppress(OSError):
                path.parent.rmdir()
        out.append(Change(target, path, "removed"))
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
                replace_file(path, stripped)
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
            # Written already for another agent that reads the same file -- but this
            # agent's own old copies still have to go.
            changes += _drop_legacy(root, target, seen, dry_run=dry_run)
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
            # and any copy left where this agent used to be looked for
            changes += [c for c in _drop_legacy(root, target, seen, dry_run=dry_run)
                        if c.path.name == "SKILL.md"]
            continue
        current = path.read_text()
        stripped = remove_block(current)
        action = "removed" if stripped != current else "unchanged"
        if action == "removed" and not dry_run:
            replace_file(path, stripped)
        changes.append(Change(target, path, action))
    return changes

