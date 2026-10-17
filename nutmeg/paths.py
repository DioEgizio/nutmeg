"""Where nutmeg keeps its files, following the XDG base directory spec.

Every location is a function rather than a constant so that it follows the
environment at the time it is asked for, which is what the tests rely on.

In the Flatpak, the XDG variables name the sandbox's own folders under
`~/.var/app/<app id>`, so nutmeg's settings, Roblox and logs live there. The
few things nutmeg shares with the rest of the desktop, Butter's data and the
icon theme, are found in the host's folders instead (`host_data_home`).

The AppImage's AppRun sets variables that point the libraries it carries at
their files. Programs nutmeg starts get the environment without them
(`host_env`).
"""

from __future__ import annotations

import os
from pathlib import Path

APP_ID = "io.github.frolleks.Nutmeg"
# Flatpak puts this file at the root of every sandbox.
FLATPAK_INFO = Path("/.flatpak-info")
# Set by the AppImage's AppRun to the variables it set. The value each had
# before is in NUTMEG_HOST_<name>, which is missing where it was unset.
APPIMAGE_VARS = "NUTMEG_APPIMAGE_VARS"
# Set by the AppImage runtime for the program it runs.
_APPIMAGE_RUNTIME_VARS = ("APPIMAGE", "APPDIR", "ARGV0", "OWD")


def in_flatpak() -> bool:
    """Whether nutmeg runs in a Flatpak sandbox."""
    return FLATPAK_INFO.is_file()


def appimage() -> Path | None:
    """The .AppImage file nutmeg runs from, or None. A program started from
    another AppImage inherits its APPIMAGE and APPDIR, so nutmeg also checks
    that it runs from inside APPDIR."""
    image = os.environ.get("APPIMAGE", "")
    appdir = os.environ.get("APPDIR", "")
    if not (os.path.isabs(image) and os.path.isabs(appdir)):
        return None
    if not Path(__file__).resolve().is_relative_to(Path(appdir).resolve()):
        return None
    return Path(image)


def host_env() -> dict[str, str]:
    """The environment for the programs nutmeg starts: nutmeg's own, with
    what the AppImage changed for its bundled libraries put back."""
    env = dict(os.environ)
    names = env.pop(APPIMAGE_VARS, None)
    if names is None:
        return env
    for name in names.split():
        saved = env.pop(f"NUTMEG_HOST_{name}", None)
        if saved is None:
            env.pop(name, None)
        else:
            env[name] = saved
    for name in _APPIMAGE_RUNTIME_VARS:
        env.pop(name, None)
    return env


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


def host_data_home() -> Path:
    """The host's `XDG_DATA_HOME`. Flatpak does not pass the host's value
    in, so in the Flatpak it is taken to be the default, ~/.local/share."""
    if in_flatpak():
        return Path.home() / ".local/share"
    return data_home()


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


def runtime_dir() -> Path:
    """A folder for sockets that every nutmeg process of this user can reach.
    In the Flatpak it is `$XDG_RUNTIME_DIR/app/<app id>`, the one folder that
    Flatpak shares between sandboxes of the same app."""
    base = _base("XDG_RUNTIME_DIR", ".cache")
    if in_flatpak():
        return base / "app" / os.environ.get("FLATPAK_ID", APP_ID)
    return base / "nutmeg"


def lock_file() -> Path:
    return data_dir() / "nutmeg.lock"


def applications_dir() -> Path:
    return data_home() / "applications"


def icons_dir() -> Path:
    """The user's part of the hicolor icon theme, which every icon theme
    falls back to. The host's, also in the Flatpak."""
    return host_data_home() / "icons" / "hicolor"
