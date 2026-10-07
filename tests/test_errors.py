"""Mistakes a person can fix end in one line that says what to fix, not a traceback.

Found in review: a typo in inventory.yaml or config.yaml surfaced as a Python traceback
with an empty stdout -- so an agent running `fleet ls --json` got nothing to read."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from fleet import config
from fleet.state import inventory as inv


def test_a_malformed_record_is_named_not_skipped(tmp_path):
    """Skipping it would be worse: the next write would save the list without it."""
    path = tmp_path / "inventory.yaml"
    path.write_text("devices:\n  - name: nas\n    id: x\n    kind: laptop\n", encoding="utf-8")
    with pytest.raises(inv.InventoryError, match="nas"):
        inv.load(path)
    path.write_text("devices:\n  - just a string\n", encoding="utf-8")
    with pytest.raises(inv.InventoryError, match="#1"):
        inv.load(path)


def test_a_setting_that_is_not_a_number_falls_back_to_its_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text("telemetry_ttl_s: 5m\nmax_workers: 3\n", encoding="utf-8")
    cfg = config.load_config()
    assert cfg.telemetry_ttl_s == 60 and cfg.max_workers == 3
    assert "telemetry_ttl_s" in capsys.readouterr().err


def test_the_cli_reports_it_in_one_line_and_as_json(tmp_path):
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "inventory.yaml").write_text("devices:\n  - name: a\n    id: x\n    kind: laptop\n",
                                                   encoding="utf-8")
    env = {"FLEET_CONFIG_DIR": str(tmp_path / "c"), "FLEET_STATE_DIR": str(tmp_path / "s"),
           "FLEET_NO_AUTO_CENTER": "1", "PYTHONUTF8": "1", "NO_COLOR": "1",
           "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    p = subprocess.run([sys.executable, "-m", "fleet", "ls", "--json"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2
    assert json.loads(p.stdout) == {"ok": False,
                                    "error": "device a in the inventory is malformed: "
                                             "'laptop' is not a valid Kind"}
    assert "Traceback" not in p.stderr
