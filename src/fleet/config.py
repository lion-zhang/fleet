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
INVENTORY_PATH = CONFIG_DIR / "inventory.yaml"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
DB_PATH = STATE_DIR / "cache.db"

DEFAULTS: dict = {
    "telemetry_ttl_s": 60,      # how long a probe result is considered fresh
    "presence_ttl_s": 10,       # tailscale presence is nearly free, so refresh often
    "probe_timeout_s": 20,
    "connect_timeout_s": 8,
    "max_workers": 8,
    "shared_min_interval_s": 300,   # never hammer a multi-user cluster
    "snapshot_retention": 120,      # ring buffer per device; enough for idle detection
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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    ensure_dirs()
    data = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            loaded = yaml.safe_load(CONFIG_PATH.read_text()) or {}
            if isinstance(loaded, dict):
                data.update(loaded)
        except yaml.YAMLError:
            pass      # a broken config must never stop you from listing devices
    return Config(data)
