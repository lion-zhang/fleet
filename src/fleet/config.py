"""Paths and tunables. Uses platformdirs so the same code works on macOS, Linux and
Windows without any per-OS path logic scattered through the codebase."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from platformdirs import PlatformDirs

_DIRS = PlatformDirs(appname="fleet", appauthor=False, roaming=False)


def _dir(var: str, default: str) -> Path:
    """An app's MCP settings pass `"FLEET_CONFIG_DIR": "~/fleet"` with no shell to expand
    it, and fleet made a folder named `~` wherever the app happened to start it -- an
    empty list of machines, a different one from each working directory."""
    value = os.environ.get(var)
    return Path(value).expanduser().absolute() if value else Path(default)


CONFIG_DIR = _dir("FLEET_CONFIG_DIR", _DIRS.user_config_dir)
STATE_DIR = _dir("FLEET_STATE_DIR", _DIRS.user_state_dir)
# The machines added by hand. Not `inventory.yaml`, which fleet 0.5 kept with the access
# list and the center's record: 0.6 starts from a clean list (docs/guides/upgrading-from-0.5.md).
INVENTORY_PATH = CONFIG_DIR / "machines.yaml"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
DB_PATH = STATE_DIR / "cache.db"

DEFAULTS: dict = {
    "telemetry_ttl_s": 60,      # how long a probe result is considered fresh
    # The longest a machine that is not answering is left alone before being tried
    # again. Waiting starts at telemetry_ttl_s / one minute and doubles per failure: a
    # dead host costs a connect timeout per attempt, and paying it on every `fleet ls`
    # made a machine being off cost seconds on every read, not just freshness.
    "offline_backoff_max_s": 1800,
    "probe_timeout_s": 20,
    "connect_timeout_s": 8,
    "max_workers": 8,
    "shared_min_interval_s": 300,   # never hammer a multi-user cluster
    "snapshot_retention": 120,      # ring buffer per device; enough for idle detection
    "repo": "",                     # where `fleet install` clones fleet from
}


@dataclass(slots=True)
class Config:
    data: dict

    def get(self, key: str):
        return self.data.get(key, DEFAULTS.get(key))

    def __getattr__(self, name: str):
        if name in DEFAULTS:
            return self.get(name)
        raise AttributeError(name)


def ensure_dirs() -> None:
    """Create fleet's directories, owner-only whatever the umask says.

    The inventory says which machines to log in to and how. Under a permissive umask --
    `docker exec` runs with 0000, and so do some service managers -- it came out
    world-writable, so any local user could point an endpoint somewhere else. The
    directory is the boundary that holds regardless of each file's own mode, and both are
    fleet's alone, so tightening them can surprise nobody.
    """
    for d in (CONFIG_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if d.stat().st_mode & 0o077:
                d.chmod(0o700)
        except OSError:
            pass                    # not ours to change; reading must still work


_WARNED: set[str] = set()
# Settings fleet 0.5 had, left in a config.yaml written for it. They did something once,
# so they are passed over quietly, as the configuration reference says.
_FROM_0_5 = {"sync_ttl_s", "access_retry_s"}


def load_config() -> Config:
    try:
        ensure_dirs()
    except OSError:
        pass        # a sandbox may let fleet read here but not write: reading must work
    data = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        # A broken config must never stop you from listing devices -- but every setting
        # in it was dropped without a word, as was a misspelt one (`max_wrkers: 4`).
        try:
            loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            loaded = {}
            _warn("", f"is not readable ({str(e).splitlines()[0]}); using the defaults")
        if isinstance(loaded, dict):
            for key in loaded:
                if key not in DEFAULTS and key not in _FROM_0_5:
                    import difflib
                    near = difflib.get_close_matches(str(key), DEFAULTS, n=1)
                    _warn(str(key), "is not a setting, so it does nothing"
                          + (f" -- did you mean {near[0]}?" if near else ""))
            data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        else:
            _warn("", "is not a list of settings (key: value); using the defaults")
    # A setting that is not a number (`telemetry_ttl_s: 5m`) used to raise on every read
    # that used it. Fall back to the default, and say so once. So does one below its
    # least: `max_workers: 0` raised in the thread pool on every `ls`, a timeout of 0
    # showed every machine offline, and `snapshot_retention: 0` deleted each reading as
    # it was stored.
    for key, default in DEFAULTS.items():
        if isinstance(default, int):
            least = _LEAST.get(key, 0)
            try:
                # `probe_timeout_s: no` is YAML for False, which int() reads as 0: every
                # machine then showed "no response within 0s", with nothing said.
                if isinstance(data[key], bool):
                    raise TypeError
                value = int(data[key])
                if value < least:
                    raise ValueError
                data[key] = value
            except (TypeError, ValueError):
                why = ("is not a number" if isinstance(data[key], bool)
                       or not isinstance(data[key], (int, float)) else f"is less than {least}")
                _warn(key, f"{data[key]!r} {why}; using {default}")
                data[key] = default
    return Config(data)


def _warn(key: str, what: str) -> None:
    """Once per run for each setting (or the file, when `key` is empty)."""
    if key in _WARNED:
        return
    import sys
    _WARNED.add(key)
    print(f"fleet: config.yaml: {key + ': ' if key else ''}{what}", file=sys.stderr)


# The least each number setting can be and still work. The rest may be 0: a
# `telemetry_ttl_s` of 0 measures on every read, which is a choice, not a mistake.
_LEAST = {"max_workers": 1, "probe_timeout_s": 1, "connect_timeout_s": 1,
          "snapshot_retention": 1}
