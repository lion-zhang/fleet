"""Paths and tunables. Uses platformdirs so the same code works on macOS, Linux and
Windows without any per-OS path logic scattered through the codebase."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from platformdirs import PlatformDirs

_DIRS = PlatformDirs(appname="fleet", appauthor=False, roaming=False)

CONFIG_DIR = Path(os.environ.get("FLEET_CONFIG_DIR") or _DIRS.user_config_dir)
STATE_DIR = Path(os.environ.get("FLEET_STATE_DIR") or _DIRS.user_state_dir)
# The machines added by hand. Not `inventory.yaml`, which fleet 0.5 kept with the access
# list and the center's record: 0.6 starts from a clean list (docs/guides/upgrading.md).
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


def load_config() -> Config:
    ensure_dirs()
    data = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data.update(loaded)
        except yaml.YAMLError:
            pass      # a broken config must never stop you from listing devices
    # A setting that is not a number (`telemetry_ttl_s: 5m`) used to raise on every read
    # that used it. Fall back to the default, and say so once.
    for key, default in DEFAULTS.items():
        if isinstance(default, int) and not isinstance(data.get(key), bool):
            try:
                data[key] = int(data[key])
            except (TypeError, ValueError):
                if key not in _WARNED:
                    import sys
                    _WARNED.add(key)
                    print(f"fleet: config.yaml: {key}: {data[key]!r} is not a number; "
                          f"using {default}", file=sys.stderr)
                data[key] = default
    return Config(data)
