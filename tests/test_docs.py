"""The documentation links to itself a lot, and a page renamed or a heading reworded
breaks those links silently: GitHub renders a dead link as an ordinary one. So every
relative link, and the heading it points at, is checked here."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = sorted([REPO / "README.md", REPO / "CONTRIBUTING.md", *(REPO / "docs").rglob("*.md")])
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)|href=\"([^\"]+)\"")


def _prose(text: str) -> str:
    """The text outside fenced code blocks, where a [x](y) is not a link."""
    return re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)


def _anchors(path: Path) -> set[str]:
    """The ids GitHub gives this page's headings: lowercased, punctuation dropped,
    spaces to hyphens, and -1, -2 ... for repeats."""
    seen: dict[str, int] = {}
    out = set()
    for line in _prose(path.read_text(encoding="utf-8")).splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if not m:
            continue
        slug = re.sub(r"[^\w\- ]", "", m.group(1).strip().lower()).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_relative_link_and_anchor_resolves(doc):
    broken = []
    for m in LINK.finditer(_prose(doc.read_text(encoding="utf-8"))):
        target = m.group(1) or m.group(2)
        if re.match(r"[a-z]+:", target):            # https:, mailto:, goose:
            continue
        path, _, anchor = target.partition("#")
        dest = (doc.parent / path).resolve() if path else doc
        if not dest.exists():
            broken.append(f"{target}: no such file")
        elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            broken.append(f"{target}: no heading #{anchor}")
    assert not broken, "\n".join(broken)


def test_every_page_is_reachable_from_the_index():
    """A page nothing links to is a page nobody finds."""
    index = (REPO / "docs" / "README.md").read_text(encoding="utf-8")
    for page in (REPO / "docs").rglob("*.md"):
        rel = page.relative_to(REPO / "docs").as_posix()
        if rel != "README.md":
            assert f"]({rel}" in index, f"docs/README.md does not link to {rel}"
