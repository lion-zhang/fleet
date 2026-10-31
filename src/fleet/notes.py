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
FIELDS = ("cost", "tags", "notes", "paths", "endpoints", "wake")
MAX_TAGS, MAX_TAG = 32, 40
MAX_NOTE = 1000
MAX_KINDS, MAX_PATHS, MAX_PATH = 12, 10, 300
# The kinds of folder fleet names in its docs and its skill. Others are kept, so a
# machine can say where its `checkpoints` go, but these are the ones agents look for.
KINDS = ("models", "datasets", "media", "scratch", "envs")
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# A model server the probe would not find by itself: on an unusual port, or one it does
# not recognise. `name: PORT` or `name: PORT/base/path`; never a key -- a server that
# wants one is shown as wanting one.
MAX_ENDPOINTS = 8
_ENDPOINT = re.compile(r"^(\d{1,5})(/[A-Za-z0-9._~/-]{0,100})?$")

# The network card `fleet wake` sends the magic packet to, when the probe's choice is not
# the one armed for it: `wake: {mac: aa:bb:cc:dd:ee:ff}`, or a list of them.
MAX_MACS = 4


def normalise_mac(text) -> str | None:
    """`AA-BB-CC-DD-EE-FF`, `aabb.ccdd.eeff`, `aabbccddeeff`, and macOS's `0:11:2:...`
    -> `aa:bb:cc:dd:ee:ff`; None for anything else, and for the all-zero and broadcast
    addresses, which no card has."""
    t = str(text or "").strip().lower()
    parts = re.split(r"[:-]", t)
    if len(parts) == 6 and all(re.fullmatch(r"[0-9a-f]{1,2}", p) for p in parts):
        hexed = "".join(p.zfill(2) for p in parts)
    elif re.fullmatch(r"[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}|[0-9a-f]{12}", t):
        hexed = t.replace(".", "")
    else:
        return None
    if hexed in ("0" * 12, "f" * 12):
        return None
    return ":".join(hexed[i:i + 2] for i in range(0, 12, 2))


TEMPLATE = """\
# What this machine says about itself. Read by every fleet that probes it.
# Edit by hand, or with `fleet edit NAME --path models=/data/models --note "..."`.
"""


def valid_kind(kind: str) -> bool:
    return bool(_KIND.match(kind))


class NotesError(ValueError):
    pass


class TooLong(NotesError):
    """A new value past what readers keep: shorter is the fix, not another place."""


def decode(b64: str) -> str:
    try:
        raw = base64.b64decode(b64.encode(), validate=False)
    except (binascii.Error, ValueError) as e:
        raise NotesError(f"unreadable: {e}") from None
    # The probe sends one byte past the limit, so a larger file is known for one. Read
    # cut at 8 KB it was a different file -- a list ending early, the keys after the cut
    # gone -- taken as what the machine said, without a word.
    if len(raw) > MAX_BYTES:
        raise NotesError(f"larger than {MAX_BYTES // 1024} KB, so it was not read")
    return raw.decode("utf-8", errors="replace")


def _printable(text: str) -> str:
    # The file is the machine's, a rental's written by its provider: an escape sequence
    # in a note would reach the terminal of whoever runs fleet show, and redraw it.
    return "".join(c if c.isprintable() else " " for c in text)


def _str(v, limit: int) -> str:
    return " ".join(_printable(str(v)).split())[:limit] if v is not None else ""


def _cost(value) -> float | None:
    """1.89, and as people write it by hand: "1.89", "$1.89"."""
    if isinstance(value, str):
        try:
            value = float(value.strip().removeprefix("$").strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e6:
        return float(value)
    return None


def check(data, skipped: list[str] | None = None) -> dict:
    """Only the fields fleet knows, each the right shape. Raises NotesError when the
    file is not a mapping at all; a single bad field is dropped, not the whole file --
    and named in `skipped`, when given: a cost written `$1.89/hr` vanished from every
    view, and nothing said why."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise NotesError("not a mapping of fields")
    skipped = [] if skipped is None else skipped
    out: dict = {}
    # `Cost: 2` or `tag: gpu` was ignored without a word, as every other key is.
    for key in [k for k in data if k not in FIELDS][:3]:
        skipped.append(f"{_str(key, 40)!r} left out: fleet reads {', '.join(FIELDS)}")
    cost = data.get("cost")
    if isinstance(cost, dict):
        cost = cost.get("usd_per_hour")
    if (usd := _cost(cost)) is not None:
        out["cost"] = {"usd_per_hour": usd}
    elif cost is not None:
        skipped.append(f"cost {_str(cost, 40)!r} left out: a cost is US dollars per hour, "
                       "like 1.75")
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = tags.split(",")
    if isinstance(tags, list):
        clean = [_str(t, MAX_TAG).lower() for t in tags if isinstance(t, (str, int, float))]
        if clean := [t for t in dict.fromkeys(clean) if t][:MAX_TAGS]:
            out["tags"] = clean
    elif tags is not None:
        skipped.append("tags left out: tags are a list, like tags: [train, nvme]")
    note = data.get("notes")
    if isinstance(note, list):
        # Several lines written as a list read back as "['a', 'b']".
        note = " ".join(str(n) for n in note if isinstance(n, (str, int, float)))
    if isinstance(note, dict):
        skipped.append("notes left out: a note is a line of text")
    elif note := _str(note, MAX_NOTE):
        out["notes"] = note
    paths = data.get("paths")
    if isinstance(paths, dict):
        kept: dict[str, list[str]] = {}
        for kind, value in list(paths.items())[:MAX_KINDS * 2]:
            kind = str(kind).strip().lower()
            if not _KIND.match(kind):
                skipped.append(f"paths {_str(kind, 40)!r} left out: a kind of folder is "
                               "one word, like models")
                continue
            if isinstance(value, dict):
                skipped.append(f"paths {kind} left out: a kind's folders are a path or a "
                               "list of them")
                continue
            items = value if isinstance(value, list) else [value]
            items = list(dict.fromkeys(
                _printable(str(p)).strip()[:MAX_PATH] for p in items
                if isinstance(p, (str, int)) and _printable(str(p)).strip()))[:MAX_PATHS]
            if items:
                kept[kind] = items
            if len(kept) >= MAX_KINDS:
                break
        if kept:
            out["paths"] = kept
    elif paths is not None:
        # `paths: [/data/models]`, with no kind: every folder in it went unseen.
        skipped.append("paths left out: paths are kinds of folder, like "
                       "paths: {models: /data/models}")
    endpoints = data.get("endpoints")
    if isinstance(endpoints, dict):
        eps: dict[str, dict] = {}
        for name, value in list(endpoints.items())[:MAX_ENDPOINTS * 2]:
            if isinstance(value, dict):
                # {port: 9000, path: /v1}: the shape `fleet show --json` prints, and so
                # the one copied into the file. Dropped, the server was not looked for.
                value = f"{value.get('port', '')}{value.get('path') or ''}"
            parsed = endpoint_spec(str(value))
            if _KIND.match(str(name).strip().lower()) and parsed:
                eps[str(name).strip().lower()] = parsed
            else:
                skipped.append(f"endpoint {_str(name, 40)} {_str(value, 40)!r} left out: a "
                               "server is a name and PORT or PORT/PATH, like mine: 9000/v1")
            if len(eps) >= MAX_ENDPOINTS:
                break
        if eps:
            out["endpoints"] = eps
    elif endpoints is not None:
        skipped.append("endpoints left out: endpoints are names and ports, like "
                       "endpoints: {mine: 9000/v1}")
    wake = data.get("wake")
    if isinstance(wake, dict):
        given = wake.get("mac")
        given = given if isinstance(given, list) else [] if given is None else [given]
        macs = []
        for value in given[:MAX_MACS * 2]:
            if (mac := normalise_mac(value)) is None:
                skipped.append(f"wake mac {_str(value, 40)!r} left out: a MAC address is "
                               "six pairs of hex digits, like aa:bb:cc:dd:ee:ff")
            elif mac not in macs:
                macs.append(mac)
        if macs := macs[:MAX_MACS]:
            out["wake"] = {"macs": macs}
    elif wake is not None:
        skipped.append("wake left out: wake is a mapping, like wake: {mac: aa:bb:cc:dd:ee:ff}")
    return out


def endpoint_spec(value: str) -> dict | None:
    """`8001` or `8001/v1` -> {"port": 8001, "path": "/v1"}; None if it is not one."""
    m = _ENDPOINT.match(value.strip())
    if not m or not 1 <= int(m.group(1)) <= 65535:
        return None
    return {"port": int(m.group(1)), "path": m.group(2) or ""}


def parse(text: str, skipped: list[str] | None = None) -> dict:
    try:
        return check(untrusted.load(text), skipped)
    except yaml.YAMLError as e:
        raise NotesError(f"not valid YAML: {str(e).splitlines()[0][:120]}") from None


def merge(system: dict, user: dict) -> dict:
    """The user's file over the machine's: field by field, for paths kind by kind, and
    tags from both."""
    out = {**system, **{k: v for k, v in user.items() if k not in ("paths", "endpoints")}}
    if system.get("tags") and user.get("tags"):
        # Labels, not a setting: `fleet edit --tag mine` writes the user's file, and the
        # admin's tags in /etc/fleet vanished from `fleet ls --tag` the moment it did.
        out["tags"] = list(dict.fromkeys(system["tags"] + user["tags"]))[:MAX_TAGS]
    for key in ("paths", "endpoints"):
        if key in system or key in user:
            out[key] = {**system.get(key, {}), **user.get(key, {})}
    return out


def from_probe(kv: dict[str, str], skipped: list[str] | None = None) -> tuple[dict, str]:
    """The merged notes from a probe's `notes.system` / `notes.user`, and any error;
    the fields left out for their shape are added to `skipped`."""
    parts, errors = [], []
    for key in ("notes.system", "notes.user"):
        if not kv.get(key):
            parts.append({})
            continue
        where = f"{'/etc/fleet' if key == 'notes.system' else 'user'} machine.yaml"
        left_out: list[str] = []
        try:
            parts.append(parse(decode(kv[key]), left_out))
        except NotesError as e:
            parts.append({})
            errors.append(f"{where}: {e}")
        if skipped is not None:
            skipped += [f"{where}: {what}" for what in left_out]
    return merge(*parts), "; ".join(errors)


def _within_limits(data: dict, *, paths, tags_add, note) -> None:
    """Refuse a new value the reader would cut: it was written whole under a ✓, and every
    reader saw less -- a note's end gone, a tag `fleet ls --tag` no longer matched.
    Only what this edit adds is checked, so a file trimmed by hand still takes edits."""
    if note and len(" ".join(note.split())) > MAX_NOTE:
        raise TooLong(f"a note is at most {MAX_NOTE} characters; this one is "
                         f"{len(' '.join(note.split()))}")
    for t in tags_add:
        if len(t) > MAX_TAG:
            raise TooLong(f"a tag is at most {MAX_TAG} characters: {t[:MAX_TAG]}…")
    if tags_add and len(data.get("tags") or []) > MAX_TAGS:
        raise TooLong(f"a machine keeps at most {MAX_TAGS} tags; this would make "
                         f"{len(data['tags'])}")
    for kind, value in (paths or {}).items():
        if value is None:
            continue
        if len(value) > MAX_PATHS:
            raise TooLong(f"at most {MAX_PATHS} folders of one kind; {kind} would have "
                             f"{len(value)}")
        for v in value:
            if len(v) > MAX_PATH:
                raise TooLong(f"a folder path is at most {MAX_PATH} characters: "
                                 f"{v[:60]}…")


def edit(text: str, *, paths: dict[str, list[str] | None] | None = None,
         cost: float | None = None, clear_cost: bool = False,
         tags_add: list[str] = (), tags_remove: list[str] = (),
         note: str | None = None,
         endpoints: dict[str, str | None] | None = None) -> str:
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
    if endpoints:
        current = data.get("endpoints") if isinstance(data.get("endpoints"), dict) else {}
        for name, value in endpoints.items():
            if value is None:
                current.pop(name, None)
            else:
                current[name] = value
        if current:
            data["endpoints"] = current
        else:
            data.pop("endpoints", None)
    if clear_cost:
        data.pop("cost", None)
    elif cost is not None:
        data["cost"] = cost
    if tags_add or tags_remove:
        tags = data.get("tags")
        if isinstance(tags, str):
            # `tags: gpu, training`, which every reader takes as two tags: `--tag x`
            # replaced both with x.
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tags = tags if isinstance(tags, list) else []
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
    _within_limits(data, paths=paths, tags_add=tags_add, note=note)
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
