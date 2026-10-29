"""Mistakes a person can fix end in one line that says what to fix, not a traceback.

Found in review: a typo in inventory.yaml or config.yaml surfaced as a Python traceback
with an empty stdout -- so an agent running `fleet ls --json` got nothing to read."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
                                    "error": f"{tmp_path / 'c' / 'machines.yaml'}: device a "
                                             "in the inventory is malformed: "
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
    (b"devices:\n- {id: a, name: box, probe_policy: sometimes}\n",
     "device box in the inventory is malformed: probe_policy 'sometimes' is not auto"),
    (b"devices:\n- {id: a, name: box, disk_paths: {a: 1}}\n",
     "device box in the inventory is malformed: disk_paths {'a': 1} is not a list"),
    (b"devices:\n- {id: a, name: box, endpoints: [{target: h, port: 99999}]}\n",
     "device box in the inventory is malformed: endpoint h: port 99999 is not a port"),
    (b"devices:\n- {id: a, name: box, endpoints: [{target: h, port: 0}]}\n",
     "device box in the inventory is malformed: endpoint h: port 0 is not a port"),
    (b"devices:\n- {id: a, name: [box]}\n",
     "device \\['box'\\] in the inventory is malformed: name \\['box'\\] is not text"),
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


def test_a_number_for_a_name_and_one_folder_for_disk_paths_are_read_as_meant(tmp_path):
    """`name: 5` stopped every view on a traceback; `disk_paths: /data` was watched
    letter by letter -- "disk path d is not there"."""
    path = tmp_path / "machines.yaml"
    path.write_text("devices:\n- {id: 7, name: 5, alias: 6, disk_paths: /data}\n",
                    encoding="utf-8")
    (dev,) = inv.load(path)
    assert (dev.id, dev.name, dev.alias, dev.disk_paths) == ("7", "5", "6", ["/data"])


def test_a_yes_or_no_is_not_taken_for_a_number(cfg_at, capsys):
    """`probe_timeout_s: no` is YAML for False, which int() reads as 0: every machine
    showed "no response within 0s", and nothing said why."""
    cfg_at.write_text("probe_timeout_s: no\nmax_workers: true\n", encoding="utf-8")
    cfg = config.load_config()
    assert (cfg.probe_timeout_s, cfg.max_workers) == (20, 8)
    # Said as what was written: "False" quoted something nobody typed.
    assert "probe_timeout_s: a yes/no value is not a number; using 20" in capsys.readouterr().err


@pytest.mark.parametrize("value", [".inf", "-.inf", ".nan"])
def test_infinity_or_nan_in_a_setting_falls_back_to_the_default(cfg_at, capsys, value):
    """`max_workers: .inf` is YAML for infinity, and int() refused it with OverflowError:
    every fleet command stopped on a traceback."""
    cfg_at.write_text(f"max_workers: {value}\n", encoding="utf-8")
    assert config.load_config().max_workers == 8
    assert "is not a number; using 8" in capsys.readouterr().err


@pytest.mark.parametrize("text,says", [
    ("max_wrkers: 4\n", "max_wrkers: is not a setting, so it does nothing -- did you mean "
                        "max_workers?"),
    ("- max_workers: 4\n", "is not a list of settings"),
    ("max_workers: [\n", "is not readable"),
])
def test_a_setting_that_does_nothing_is_said(cfg_at, capsys, text, says):
    """A misspelt setting, a file that is not settings at all, or one that does not
    parse: each was dropped without a word, and the defaults used instead."""
    cfg_at.write_text(text, encoding="utf-8")
    assert config.load_config().max_workers == 8
    assert says in capsys.readouterr().err


def test_a_setting_left_from_0_5_is_passed_over_quietly(cfg_at, capsys):
    cfg_at.write_text("sync_ttl_s: 300\naccess_retry_s: 300\n", encoding="utf-8")
    config.load_config()
    assert capsys.readouterr().err == ""


def test_a_folder_named_with_a_tilde_is_in_the_home_folder(tmp_path):
    """An app's MCP settings pass `"FLEET_CONFIG_DIR": "~/fleet"` with no shell to
    expand it: fleet made a folder named `~` in whatever folder it was started from."""
    import os

    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir(), work.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "FLEET_CONFIG_DIR": "~/fleet-c", "FLEET_STATE_DIR": "fleet-s",
           "FLEET_NO_FIRST_RUN": "1", "PYTHONUTF8": "1", "NO_COLOR": "1"}
    p = subprocess.run([sys.executable, "-c", "from fleet import config; "
                        "print(config.CONFIG_DIR); print(config.STATE_DIR)"],
                       env=env, cwd=work, capture_output=True, text=True, timeout=60)
    cfg, state = p.stdout.split()
    assert Path(cfg).resolve() == (home / "fleet-c").resolve()
    assert Path(state).is_absolute()
    assert Path(state).resolve() == (work / "fleet-s").resolve()


def test_a_folder_it_cannot_write_is_said_in_one_line_and_as_json(tmp_path):
    """A read-only home, a sandbox, a full disk: `fleet add`, `rm`, `edit --local` and
    `ls --discover --hide` ended in a traceback, and an agent got an empty stdout."""
    import os

    blocker = tmp_path / "a-file"
    blocker.write_text("")
    env = {**os.environ, "FLEET_CONFIG_DIR": str(blocker / "c"),
           "FLEET_STATE_DIR": str(tmp_path / "s"), "FLEET_NO_FIRST_RUN": "1",
           "PYTHONUTF8": "1", "NO_COLOR": "1"}
    p = subprocess.run([sys.executable, "-m", "fleet", "add", "--self", "--json"], env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 2, p.stderr
    said = json.loads(p.stdout)
    assert said["ok"] is False and said["error"].startswith("cannot write ")
    assert str(blocker / "c") in said["error"] and "Errno" not in said["error"]
    assert "Traceback" not in p.stderr


def test_a_first_run_that_cannot_write_lets_the_command_run(monkeypatch):
    """A sandbox that lets fleet read its state folder but not write it: the first run
    could not take its turn, and every command died there without running."""
    from fleet.ops import firstrun
    from fleet.state import writes

    monkeypatch.delenv("FLEET_NO_FIRST_RUN", raising=False)
    monkeypatch.setattr(firstrun, "_marker", lambda: Path("/nonexistent/first-run-done"))
    monkeypatch.setattr("fleet.config.ensure_dirs", lambda: None)

    def read_only(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(writes, "_publish", read_only)
    assert firstrun.maybe("ls", lambda *a, **k: None) is False


def test_an_entry_cut_off_mid_write_names_the_file_and_what_is_missing(tmp_path):
    """"Device.__init__() missing 1 required positional argument: 'name'", of "the
    inventory", was all a list cut off mid-write said."""
    from fleet.state import inventory as inv
    path = tmp_path / "machines.yaml"
    path.write_text("devices:\n- id: a\n  name: box\n- id: linux:machine-i\n", encoding="utf-8")
    with pytest.raises(inv.InventoryError) as exc:
        inv.load(path)
    assert str(exc.value).startswith(str(path))
    assert "device #2" in str(exc.value) and "it has no name" in str(exc.value)
    assert "__init__" not in str(exc.value)


def test_a_config_folder_that_is_a_file_is_said_to_be_one(tmp_path):
    """FLEET_CONFIG_DIR naming a file said "cannot write X (File exists)"."""
    from fleet.state.writes import CannotWrite, atomic_write

    afile = tmp_path / "cfg"
    afile.write_text("")
    with pytest.raises(CannotWrite, match="is a file, where fleet needs a folder"):
        atomic_write(afile / "machines.yaml", "version: 1\n")


@pytest.mark.parametrize("value", ["5", "yes", "[a]"])
def test_a_repo_that_is_not_text_is_not_cloned(cfg_at, capsys, value):
    """`repo: 5` became the URL "5", and `fleet install` tried to clone it."""
    cfg_at.write_text(f"repo: {value}\n", encoding="utf-8")
    assert config.load_config().get("repo") == ""
    assert "repo: " in capsys.readouterr().err


@pytest.mark.parametrize("text, said", [
    (b"devices:\n- {id: a, name: lab}\n- {id: b, name: lab}\n", "answer to 'lab'"),
    (b"devices:\n- {id: a, name: lab}\n- {id: b, name: gpu, alias: lab}\n", "answer to 'lab'"),
    (b"devices:\n- {id: a, name: lab}\n- {id: a, name: gpu}\n", "machine a is in the inventory twice"),
])
def test_two_entries_that_answer_to_one_name_are_named(tmp_path, text, said):
    """Two lists merged in dotfiles, or an entry copied by hand: both loaded, and every
    command took the first -- `fleet ssh lab` reached the other machine."""
    path = tmp_path / "machines.yaml"
    path.write_bytes(text)
    with pytest.raises(inv.InventoryError, match=said):
        inv.load(path)


def test_a_removed_machine_and_its_alias_naming_itself_are_not_a_clash(tmp_path):
    path = tmp_path / "machines.yaml"
    path.write_bytes(b"devices:\n- {id: a, name: lab, deleted_at: 5}\n"
                     b"- {id: b, name: lab, alias: lab}\n")
    assert [d.id for d in inv.live(inv.load(path))] == ["b"]
