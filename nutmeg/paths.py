"""Where nutmeg keeps its files, following the XDG base directory spec.

Every location is a function rather than a constant so that it follows the
environment at the time it is asked for, which is what the tests rely on.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_ID = "io.github.frolleks.Nutmeg"


def _base(variable: str, fallback: str) -> Path:
    # The spec says a relative value is invalid and must be ignored.
    value = os.environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_dir() -> Path:
    return _base("XDG_CONFIG_HOME", ".config") / "nutmeg"


def data_home() -> Path:
    """`XDG_DATA_HOME`, shared with other programs."""
    return _base("XDG_DATA_HOME", ".local/share")


def data_dir() -> Path:
    return data_home() / "nutmeg"


def cache_dir() -> Path:
    return _base("XDG_CACHE_HOME", ".cache") / "nutmeg"


def state_dir() -> Path:
    return _base("XDG_STATE_HOME", ".local/state") / "nutmeg"


def config_file() -> Path:
    return config_dir() / "config.json"


def installed_file() -> Path:
    """Which Roblox build is current, for launching when the network is down."""
    return data_dir() / "installed.json"


def versions_dir() -> Path:
    return data_dir() / "versions"


def default_prefix() -> Path:
    return data_dir() / "prefix"


def downloads_dir() -> Path:
    return cache_dir() / "downloads"


def logs_dir() -> Path:
    return state_dir() / "logs"


def lock_file() -> Path:
    return data_dir() / "nutmeg.lock"


def applications_dir() -> Path:
    return data_home() / "applications"
