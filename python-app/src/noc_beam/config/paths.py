"""Per-user data and config paths for NOC_Beam."""
from __future__ import annotations

from pathlib import Path

from platformdirs import PlatformDirs

_dirs = PlatformDirs(appname="NOC_Beam", appauthor=False, roaming=True)


def config_dir() -> Path:
    p = Path(_dirs.user_config_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    p = Path(_dirs.user_data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = Path(_dirs.user_log_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return config_dir() / "settings.json"


def accounts_file() -> Path:
    return config_dir() / "accounts.json"
