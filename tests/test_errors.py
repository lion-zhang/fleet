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


@pytest.fixture
def cfg_at(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "_WARNED", set())
    return tmp_path / "config.yaml"


def test_a_setting_below_its_least_falls_back_too(cfg_at, capsys):
    """`max_workers: 0` raised inside the thread pool on every `ls`; a timeout of 0
    showed every machine offline; `snapshot_retention: 0` kept no reading at all."""
    cfg_at.write_text("max_workers: 0\nprobe_timeout_s: -1\nconnect_timeout_s: 0\n"
                      "snapshot_retention: 0\ntelemetry_ttl_s: 0\n", encoding="utf-8")
    cfg = config.load_config()
    assert (cfg.max_workers, cfg.probe_timeout_s, cfg.connect_timeout_s,
            cfg.snapshot_retention) == (8, 20, 8, 120)
    assert cfg.telemetry_ttl_s == 0, "measuring on every read is a choice"
    err = capsys.readouterr().err
    assert "max_workers: 0 is less than 1; using 8" in err


@pytest.mark.parametrize("make", [
    lambda p: p.write_bytes(b"max_workers: 4\n\xff\xfe\n"),     # not UTF-8
    lambda p: p.mkdir(),                                            # not a file
])
def test_an_unreadable_config_is_ignored_as_the_docs_say(cfg_at, make):
    make(cfg_at)
    assert config.load_config().max_workers == 8


def test_the_cli_reports_it_in_one_line_and_as_json(tmp_path):
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "machines.yaml").write_text("devices:\n  - name: a\n    id: x\n    kind: laptop\n",
                                                   encoding="utf-8")
    env = {"FLEET_CONFIG_DIR": str(tmp_path / "c"), "FLEET_STATE_DIR": str(tmp_path / "s"),
           "FLEET_NO_FIRST_RUN": "1", "PYTHONUTF8": "1", "NO_COLOR": "1",
           "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    p = subprocess.run([sys.executable, "-m", "fleet", "ls", "--json"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2
    assert json.loads(p.stdout) == {"ok": False,
                                    "error": "device a in the inventory is malformed: "
                                             "'laptop' is not a valid Kind"}
    assert "Traceback" not in p.stderr


@pytest.mark.parametrize("text, said", [
    (b"devices: \xff\n", "is not UTF-8 text"),
    (b"devices:\n- {id: a, name: box, endpoints: [{target: h, port: 22a}]}\n",
     "device box in the inventory is malformed: endpoint h: port '22a' is not a number"),
    (b"devices:\n- {id: a, name: box, endpoints: [h.example]}\n",
     "device box in the inventory is malformed: each endpoint must be a mapping"),
    (b"devices:\n- {id: a, name: box, cost: 1.5}\n",
     "device box in the inventory is malformed: cost 1.5 is not `usd_per_hour: N`"),
    (b"devices:\n- {id: a, name: box, cost: {usd_per_hour: lots}}\n",
     "device box in the inventory is malformed: cost 'lots' is not US dollars per hour"),
    (b"devices:\n- {id: a, name: box, tags: {gpu: 1}}\n",
     "device box in the inventory is malformed: tags {'gpu': 1} is not a list"),
])
def test_a_hand_edited_machines_file_is_named_not_a_traceback(tmp_path, text, said):
    """machines.yaml is meant to be fixable by hand, and shared with dotfiles: a bad byte
    or a mistyped port raised a traceback from deep inside whatever command ran."""
    path = tmp_path / "machines.yaml"
    path.write_bytes(text)
    with pytest.raises(inv.InventoryError, match=said.replace("(", r"\(")):
        inv.load(path)


def test_numbers_typed_into_machines_yaml_are_read_as_they_were_meant(tmp_path):
    """YAML reads `7` as an int and `"1.50"` as a string; the views then failed on a tag
    with no .strip() and a cost that `:.2f` cannot format."""
    path = tmp_path / "machines.yaml"
    path.write_text('devices:\n- {id: a, name: box, tags: [gpu, 7], '
                    'cost: {usd_per_hour: "1.50"}}\n', encoding="utf-8")
    (dev,) = inv.load(path)
    assert dev.tags == ["gpu", "7"] and dev.cost["usd_per_hour"] == 1.5
