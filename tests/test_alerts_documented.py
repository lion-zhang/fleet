"""Every alert fleet raises is in the reference's table of alerts."""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
VIEW = ROOT / "src" / "fleet" / "render" / "view.py"
FACTS = (ROOT / "docs" / "reference" / "facts.md").read_text(encoding="utf-8")


def _alert_words() -> list[str]:
    """The fixed words of each alert: the text between its placeholders."""
    tree = ast.parse(VIEW.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_alerts")
    words = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words += [w.strip(" -:`") for w in node.value.split("`")]
    # A key read from the reading (`notes_error`) is not words of an alert.
    return [w for w in words if " " in w and sum(c.isalpha() for c in w) >= 8]


def test_every_alert_is_in_the_reference():
    # Three were missing ("disk path ... is not there", the "(when last read)" ending,
    # a machine.yaml field "left out"), so an agent reading the table did not know them.
    words = _alert_words()
    assert len(words) >= 8
    missing = [w for w in words if w not in FACTS]
    assert not missing, missing
