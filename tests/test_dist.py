"""The files fleet ships for installing it from inside an agent are generated
(scripts/build_dist.py). A copy that drifted from the source is a skill telling agents
something fleet no longer does, or a manifest naming a release that is not the latest."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _build():
    spec = importlib.util.spec_from_file_location("build_dist", REPO / "scripts" / "build_dist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("rel", sorted(_build().outputs()))
def test_the_committed_copy_is_what_the_source_generates(rel):
    assert (REPO / rel).read_text(encoding="utf-8") == _build().outputs()[rel], \
        f"{rel} is stale -- run: uv run python scripts/build_dist.py"


def test_every_manifest_names_the_package_and_its_version():
    b = _build()
    v = b.version()
    for rel in (".claude-plugin/plugin.json", "gemini-extension.json", "server.json",
                "mcpb/manifest.json"):
        assert json.loads((REPO / rel).read_text(encoding="utf-8"))["version"] == v, rel
    server = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    assert server["packages"][0]["identifier"] == "agents-fleet"
    assert len(server["description"]) <= 100, "the MCP Registry's limit"
    # The registry proves we own the PyPI package by finding this in its README.
    assert f"<!-- mcp-name: {server['name']} -->" in (REPO / "README.md").read_text(encoding="utf-8")


def _hook_says(path_dirs: list[str], home: Path) -> str:
    hook = json.loads((REPO / "hooks/hooks.json").read_text(encoding="utf-8"))
    cmd = hook["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    env = {"PATH": os.pathsep.join(path_dirs), "HOME": str(home)}
    r = subprocess.run(["/bin/sh", "-c", cmd], env=env, capture_output=True, text=True)
    assert r.returncode == 0, "a failing hook is an error in every session"
    return r.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_the_plugin_hook_speaks_only_when_fleet_is_missing(tmp_path):
    said = _hook_says(["/usr/bin", "/bin"], tmp_path)
    assert "install.sh | sh" in said and "Do not install it unasked" in said
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "fleet").write_text("#!/bin/sh\n")
    (bin_ / "fleet").chmod(0o755)
    assert _hook_says([str(bin_), "/usr/bin", "/bin"], tmp_path) == ""
    # Installed, but in a session whose PATH never read a profile.
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (bin_ / "fleet").rename(local / "fleet")
    assert _hook_says(["/usr/bin", "/bin"], tmp_path) == ""


def test_the_shipped_skill_says_what_to_do_without_fleet():
    text = (REPO / "skills/fleet/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: fleet\ndescription: ")
    assert "uv tool install agents-fleet" in text and "`fleet ls --json`" in text


def test_every_install_link_launches_the_published_package():
    """Install links carry the launch line inside a URL, some base64-encoded (Cursor, LM
    Studio). A rename of the package missed those: they launched `agent-fleet`, which is
    not on PyPI, and the button installed a server that could never start."""
    import base64
    import re
    import urllib.parse

    from fleet.links import PACKAGE

    found = 0
    for doc in [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]:
        for url in re.findall(r"https?://[^\s)\"'>]+", doc.read_text(encoding="utf-8")):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            for value in query.get("config", []):
                try:
                    config = json.loads(value)
                except ValueError:
                    config = json.loads(base64.b64decode(value))
                found += 1
                assert config.get("args", [])[:1] == [PACKAGE], (doc.name, config)
    assert found >= 4, "README and docs/guides/agents.md carry install links"
