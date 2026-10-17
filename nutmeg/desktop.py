"""Menu entries, and registering nutmeg for Roblox's URL schemes.

Two desktop entries are written to $XDG_DATA_HOME/applications: one that
starts Roblox and handles `roblox-player:` and `roblox:` links, and one that
opens the settings. `xdg-mime` makes the first the default for both schemes.
The first also offers the settings in its right-click menu, as an action.

Their icons go in the user's hicolor icon theme, named after the entries:
the first shows Roblox's own icon, copied out of the installed build's
AppIcon.icns, and the second nutmeg's. An entry installed before Roblox shows
`applications-games` until the first launch installs a build.
"""

from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import paths, roblox
from .errors import NutmegError
from .protocol import SCHEMES

PLAYER_ID = f"{paths.APP_ID}.desktop"
SETTINGS_ID = f"{paths.APP_ID}.Settings.desktop"
MIME_TYPES = [f"x-scheme-handler/{scheme}" for scheme in SCHEMES]
SETTINGS_ACTION = "open-settings"

PLAYER_ICON = paths.APP_ID
SETTINGS_ICON = f"{paths.APP_ID}.Settings"
# The player entry's icon while no Roblox build, and so no Roblox icon, is installed.
FALLBACK_ICON = "applications-games"
# The fixed sizes hicolor's index.theme has folders for. An icon in a folder
# the theme does not list is never found.
THEME_SIZES = (16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256, 512)

# Characters that make the Desktop Entry spec require an argument be quoted.
_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def quote_exec_argument(argument: str) -> str:
    """One argument for an `Exec` key, as the Desktop Entry spec quotes it,
    before the key file's own escaping (see `exec_value`)."""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in argument):
        raise NutmegError(f"Cannot put a control character in a desktop entry: {argument!r}")
    if any(c in _RESERVED for c in argument):
        escaped = "".join("\\" + c if c in '"`$\\' else c for c in argument)
        argument = f'"{escaped}"'
    return argument.replace("%", "%%")


def exec_value(arguments: list[str], field_code: str | None = None) -> str:
    """An `Exec` value running `arguments`, followed by a field code such as
    `%u` if given. The key file's escaping of backslashes is applied last."""
    words = [quote_exec_argument(a) for a in arguments]
    if field_code:
        words.append(field_code)
    return " ".join(words).replace("\\", "\\\\")


def _on_default_path(directory: Path) -> bool:
    candidates = list(site.getsitepackages())
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        candidates.append(user_site)
    real = os.path.realpath(directory)
    return any(os.path.realpath(c) == real for c in candidates)


def nutmeg_command() -> list[str]:
    """How to run this copy of nutmeg from a desktop entry.

    The interpreter running now is used, so a virtual environment keeps
    working. A copy that is not installed (a source checkout) is found through
    PYTHONPATH.
    """
    if not sys.executable:
        raise NutmegError("Cannot tell which Python interpreter is running nutmeg.")
    package_parent = Path(__file__).resolve().parent.parent
    command = [sys.executable, "-m", "nutmeg"]
    if not _on_default_path(package_parent):
        command = ["env", f"PYTHONPATH={package_parent}"] + command
    return command


def _group(header: str, fields: list[tuple[str, str]]) -> str:
    lines = [f"[{header}]"] + [f"{key}={value}" for key, value in fields]
    return "\n".join(lines) + "\n"


def player_entry(command: list[str], icon: str = PLAYER_ICON) -> str:
    entry = _group(
        "Desktop Entry",
        [
            ("Type", "Application"),
            ("Version", "1.0"),
            ("Name", "Roblox (nutmeg)"),
            ("Comment", "Play Roblox with the macOS client through rosemary"),
            ("Exec", exec_value(command + ["launch"], "%u")),
            ("Icon", icon),
            ("Terminal", "false"),
            ("Categories", "Game;"),
            ("MimeType", "".join(f"{m};" for m in MIME_TYPES)),
            ("StartupNotify", "true"),
            ("Actions", f"{SETTINGS_ACTION};"),
        ]
    )
    action = _group(
        f"Desktop Action {SETTINGS_ACTION}",
        [
            ("Name", "nutmeg Settings"),
            ("Exec", exec_value(command + ["settings"])),
            ("Icon", SETTINGS_ICON),
        ]
    )
    return entry + "\n" + action


def settings_entry(command: list[str]) -> str:
    return _group(
        "Desktop Entry",
        [
            ("Type", "Application"),
            ("Version", "1.0"),
            ("Name", "nutmeg Settings"),
            ("Comment", "Configure how nutmeg installs and runs Roblox"),
            ("Exec", exec_value(command + ["settings"])),
            ("Icon", SETTINGS_ICON),
            ("Terminal", "false"),
            ("Categories", "Game;"),
            ("StartupNotify", "true"),
        ]
    )


def _refresh_database(directory: Path) -> None:
    tool = shutil.which("update-desktop-database")
    if tool:
        subprocess.run([tool, "-q", str(directory)], check=False, stdin=subprocess.DEVNULL)


def _player_icon(size: int) -> Path:
    return paths.icons_dir() / f"{size}x{size}" / "apps" / f"{PLAYER_ICON}.png"


def _settings_icon() -> Path:
    return paths.icons_dir() / "scalable" / "apps" / f"{SETTINGS_ICON}.svg"


def _icon_files() -> list[Path]:
    return [_settings_icon()] + [_player_icon(size) for size in THEME_SIZES]


def _player_icon_sizes() -> list[int]:
    return [size for size in THEME_SIZES if _player_icon(size).is_file()]


def _write_if_changed(path: Path, data: bytes) -> bool:
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def _refresh_icons() -> None:
    # A newer folder time makes GTK, KDE and others look through the theme
    # again, and makes GTK ignore an icon-theme.cache older than the change.
    try:
        os.utime(paths.icons_dir())
    except OSError:
        pass


def _install_roblox_icon(bundle: Path) -> bool:
    """Copy the build's icon into the icon theme, at each size its .icns has
    and the theme has a folder for. Returns whether a file changed."""
    icns = roblox.icon_file(bundle)
    try:
        images = roblox.icns_pngs(icns.read_bytes())
    except OSError as e:
        raise NutmegError(f"Could not read {icns}: {e.strerror or e}") from e
    if not any(size in images for size in THEME_SIZES):
        raise NutmegError(f"{icns} has no PNG image at a size the icon theme has")
    changed = False
    for size in THEME_SIZES:
        path = _player_icon(size)
        if size in images:
            changed |= _write_if_changed(path, images[size])
        elif path.exists():
            # Left by an earlier build whose icon had this size.
            path.unlink()
            changed = True
    return changed


def use_roblox_icon(bundle: Path) -> bool:
    """Give the installed player entry the icon of the Roblox build in
    `bundle`. Does nothing when the entries are not installed. Returns whether
    anything changed."""
    entry = paths.applications_dir() / PLAYER_ID
    if not entry.is_file():
        return False
    changed = _install_roblox_icon(bundle)
    if changed:
        _refresh_icons()
    text = entry.read_text("utf-8")
    fallback = f"\nIcon={FALLBACK_ICON}\n"
    if fallback in text:
        # Only the Icon line changes: the Exec line stays the command that
        # installed the entry, which may not be the one running now.
        entry.write_text(text.replace(fallback, f"\nIcon={PLAYER_ICON}\n"), "utf-8")
        _refresh_database(entry.parent)
        changed = True
    return changed


def install() -> list[str]:
    """Write both entries and their icons, and make nutmeg the handler for
    Roblox links. Returns what was done, one line each."""
    xdg_mime = shutil.which("xdg-mime")
    if xdg_mime is None:
        raise NutmegError("xdg-mime was not found. Install xdg-utils, then try again.")
    directory = paths.applications_dir()
    directory.mkdir(parents=True, exist_ok=True)
    command = nutmeg_command()
    done = []
    # Read through importlib, which also reads it out of the zipapp.
    svg = (resources.files(__package__) / "data" / "nutmeg.svg").read_bytes()
    _write_if_changed(_settings_icon(), svg)
    done.append(f"wrote {_settings_icon()}")
    build = roblox.load_installed()
    if build is not None and build.bundle().is_dir():
        try:
            _install_roblox_icon(build.bundle())
        except NutmegError as e:
            done.append(f"could not copy Roblox's icon: {e}")
    sizes = _player_icon_sizes()
    if sizes:
        folders = ", ".join(f"{size}x{size}" for size in sizes)
        done.append(f"wrote Roblox's icon to {paths.icons_dir()} ({folders})")
    else:
        done.append(f"Roblox's icon replaces {FALLBACK_ICON} once a launch installs Roblox")
    _refresh_icons()
    player_icon = PLAYER_ICON if sizes else FALLBACK_ICON
    for name, text in ((PLAYER_ID, player_entry(command, player_icon)), (SETTINGS_ID, settings_entry(command))):
        path = directory / name
        path.write_text(text, "utf-8")
        done.append(f"wrote {path}")
    _refresh_database(directory)
    for mime in MIME_TYPES:
        result = subprocess.run(
            [xdg_mime, "default", PLAYER_ID, mime],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise NutmegError(
                f"xdg-mime could not set the handler for {mime}: "
                + (result.stderr.strip() or f"exit status {result.returncode}")
            )
        done.append(f"{mime} opens with {PLAYER_ID}")
    return done


def uninstall() -> list[str]:
    directory = paths.applications_dir()
    done = []
    for name in (PLAYER_ID, SETTINGS_ID):
        path = directory / name
        if path.exists():
            path.unlink()
            done.append(f"removed {path}")
    if done:
        _refresh_database(directory)
    icons = [path for path in _icon_files() if path.exists()]
    for path in icons:
        path.unlink()
    if icons:
        _refresh_icons()
        done.append(f"removed nutmeg's icons from {paths.icons_dir()}")
    return done


def installed() -> bool:
    return (paths.applications_dir() / PLAYER_ID).is_file()


def default_handler(mime: str) -> str | None:
    """The desktop entry that opens `mime` now, as `xdg-mime` reports it."""
    xdg_mime = shutil.which("xdg-mime")
    if xdg_mime is None:
        return None
    try:
        result = subprocess.run(
            [xdg_mime, "query", "default", mime],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    answer = result.stdout.strip()
    return answer or None
