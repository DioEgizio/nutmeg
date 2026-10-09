"""Menu entries, and registering nutmeg for Roblox's URL schemes.

Two desktop entries are written to $XDG_DATA_HOME/applications: one that
starts Roblox and handles `roblox-player:` and `roblox:` links, and one that
opens the settings. `xdg-mime` makes the first the default for both schemes.
"""

from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

from . import paths
from .errors import NutmegError
from .protocol import SCHEMES

PLAYER_ID = f"{paths.APP_ID}.desktop"
SETTINGS_ID = f"{paths.APP_ID}.Settings.desktop"
MIME_TYPES = [f"x-scheme-handler/{scheme}" for scheme in SCHEMES]

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


def _entry(fields: list[tuple[str, str]]) -> str:
    lines = ["[Desktop Entry]"] + [f"{key}={value}" for key, value in fields]
    return "\n".join(lines) + "\n"


def player_entry(command: list[str]) -> str:
    return _entry(
        [
            ("Type", "Application"),
            ("Version", "1.0"),
            ("Name", "Roblox (nutmeg)"),
            ("Comment", "Play Roblox with the macOS client through rosetta3"),
            ("Exec", exec_value(command + ["launch"], "%u")),
            ("Icon", "applications-games"),
            ("Terminal", "false"),
            ("Categories", "Game;"),
            ("MimeType", "".join(f"{m};" for m in MIME_TYPES)),
            ("StartupNotify", "true"),
        ]
    )


def settings_entry(command: list[str]) -> str:
    return _entry(
        [
            ("Type", "Application"),
            ("Version", "1.0"),
            ("Name", "nutmeg Settings"),
            ("Comment", "Configure how nutmeg installs and runs Roblox"),
            ("Exec", exec_value(command + ["settings"])),
            ("Icon", "applications-games"),
            ("Terminal", "false"),
            ("Categories", "Game;"),
            ("StartupNotify", "true"),
        ]
    )


def _refresh_database(directory: Path) -> None:
    tool = shutil.which("update-desktop-database")
    if tool:
        subprocess.run([tool, "-q", str(directory)], check=False, stdin=subprocess.DEVNULL)


def install() -> list[str]:
    """Write both entries and make nutmeg the handler for Roblox links.
    Returns what was done, one line each."""
    xdg_mime = shutil.which("xdg-mime")
    if xdg_mime is None:
        raise NutmegError("xdg-mime was not found. Install xdg-utils, then try again.")
    directory = paths.applications_dir()
    directory.mkdir(parents=True, exist_ok=True)
    command = nutmeg_command()
    done = []
    for name, text in ((PLAYER_ID, player_entry(command)), (SETTINGS_ID, settings_entry(command))):
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
