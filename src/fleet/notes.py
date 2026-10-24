"""What a machine says about itself: its machine.yaml.

A machine's cost, its tags, a line of notes, and where its models, datasets and media
live are facts about that machine, so they are kept on it -- `/etc/fleet/machine.yaml`
for the whole machine, the fleet config folder in a user's home for whoever logs in --
and every probe reads them. Whoever looks at the machine, from wherever, sees the same
answer, and there is nothing to copy between the machines that look.

The file was written by whoever controls that machine, which on a rental includes the
provider, and what it says reaches agents. So it is read like anything else from the
network (no aliases, a size limit) and only the fields below survive, each checked and
cut to size.
"""

from __future__ import annotations

import base64
import binascii
import re

import yaml

from .state import untrusted

MAX_BYTES = 8192
MAX_TAGS, MAX_TAG = 32, 40
MAX_NOTE = 1000
MAX_KINDS, MAX_PATHS, MAX_PATH = 12, 10, 300
# The kinds of folder fleet names in its docs and its skill. Others are kept, so a
# machine can say where its `checkpoints` go, but these are the ones agents look for.
KINDS = ("models", "datasets", "media", "scratch", "envs")
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

TEMPLATE = """\
# What this machine says about itself. Read by every fleet that probes it.
# Edit by hand, or with `fleet edit NAME --path models=/data/models`.
"""


def valid_kind(kind: str) -> bool:
    return bool(_KIND.match(kind))


class NotesError(ValueError):
    pass


def decode(b64: str) -> str:
    try:
        return base64.b64decode(b64.encode(), validate=False)[:MAX_BYTES].decode(
            "utf-8", errors="replace")
    except (binascii.Error, ValueError) as e:
        raise NotesError(f"unreadable: {e}") from None


def _str(v, limit: int) -> str:
    return " ".join(str(v).split())[:limit] if v is not None else ""


def check(data) -> dict:
    """Only the fields fleet knows, each the right shape. Raises NotesError when the
    file is not a mapping at all; a single bad field is dropped, not the whole file."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise NotesError("not a mapping of fields")
    out: dict = {}
    cost = data.get("cost")
    if isinstance(cost, dict):
        cost = cost.get("usd_per_hour")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 <= cost < 1e6:
        out["cost"] = {"usd_per_hour": float(cost)}
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = tags.split(",")
    if isinstance(tags, list):
        clean = [_str(t, MAX_TAG).lower() for t in tags if isinstance(t, (str, int))]
        if clean := [t for t in dict.fromkeys(clean) if t][:MAX_TAGS]:
            out["tags"] = clean
    if note := _str(data.get("notes"), MAX_NOTE):
        out["notes"] = note
    paths = data.get("paths")
    if isinstance(paths, dict):
        kept: dict[str, list[str]] = {}
        for kind, value in list(paths.items())[:MAX_KINDS * 2]:
            kind = str(kind).strip().lower()
            if not _KIND.match(kind):
                continue
            items = value if isinstance(value, list) else [value]
            items = [str(p).strip()[:MAX_PATH] for p in items
                     if isinstance(p, (str, int)) and str(p).strip()][:MAX_PATHS]
            if items:
                kept[kind] = items
            if len(kept) >= MAX_KINDS:
                break
        if kept:
            out["paths"] = kept
    return out


def parse(text: str) -> dict:
    try:
        return check(untrusted.load(text))
    except yaml.YAMLError as e:
        raise NotesError(f"not valid YAML: {str(e).splitlines()[0][:120]}") from None


def merge(system: dict, user: dict) -> dict:
    """The user's file over the machine's: field by field, and for paths kind by kind."""
    out = {**system, **{k: v for k, v in user.items() if k != "paths"}}
    if "paths" in system or "paths" in user:
        out["paths"] = {**system.get("paths", {}), **user.get("paths", {})}
    return out


def from_probe(kv: dict[str, str]) -> tuple[dict, str]:
    """The merged notes from a probe's `notes.system` / `notes.user`, and any error."""
    parts, errors = [], []
    for key in ("notes.system", "notes.user"):
        if not kv.get(key):
            parts.append({})
            continue
        try:
            parts.append(parse(decode(kv[key])))
        except NotesError as e:
            parts.append({})
            errors.append(f"{'/etc/fleet' if key == 'notes.system' else 'user'} machine.yaml: {e}")
    return merge(*parts), "; ".join(errors)


def edit(text: str, *, paths: dict[str, list[str] | None] | None = None,
         cost: float | None = None, clear_cost: bool = False,
         tags_add: list[str] = (), tags_remove: list[str] = (),
         note: str | None = None) -> str:
    """The file with these changes made, everything else in it kept as it was read.

    Applied to the text read just before writing, so a change somebody else made in the
    meantime is kept, not overwritten. A `paths` value of None removes that kind.
    """
    try:
        data = untrusted.load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise NotesError(f"machine.yaml there is not valid YAML, so it was left alone: "
                         f"{str(e).splitlines()[0][:120]}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise NotesError("machine.yaml there is not a mapping, so it was left alone")
    if paths:
        current = data.get("paths") if isinstance(data.get("paths"), dict) else {}
        for kind, value in paths.items():
            if value is None:
                current.pop(kind, None)
            else:
                current[kind] = value
        if current:
            data["paths"] = current
        else:
            data.pop("paths", None)
    if clear_cost:
        data.pop("cost", None)
    elif cost is not None:
        data["cost"] = cost
    if tags_add or tags_remove:
        tags = data.get("tags") if isinstance(data.get("tags"), list) else []
        tags = [t for t in tags if str(t).lower() not in {x.lower() for x in tags_remove}]
        tags += [t for t in tags_add if t.lower() not in {str(x).lower() for x in tags}]
        if tags:
            data["tags"] = tags
        else:
            data.pop("tags", None)
    if note is not None:
        if note:
            data["notes"] = note
        else:
            data.pop("notes", None)
    check(data)                       # never write what the next probe would reject
    body = untrusted.dump(data, sort_keys=False, default_flow_style=False,
                          allow_unicode=True) if data else ""
    # The comment block at the top of the file is kept; a new file gets one saying what
    # it is. (Comments further down do not survive: YAML has no place to keep them.)
    comments = _leading_comments(text)
    head = "".join(ln + "\n" for ln in comments) if comments else TEMPLATE
    return head + body


def _leading_comments(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        if ln.startswith("#") or not ln.strip():
            out.append(ln)
        else:
            break
    while out and not out[-1].strip():
        out.pop()
    return out
