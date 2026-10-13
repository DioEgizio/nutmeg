"""The rosemary runners and bottles Butter has installed.

Butter (https://github.com/microlift/butter) manages rosemary the way Bottles
manages Wine. It keeps each runner in `<data>/butter/runners/<id>/`, with a
`runner.toml` naming its executable, and each bottle in
`<data>/butter/bottles/<id>/`, with a `bottle.toml` and the rosemary prefix in
`prefix/`. `<data>` is `XDG_DATA_HOME`, or `~/.var/app/<id>/data` for the
Flatpak. nutmeg reads these files to offer Butter's runners and bottles in its
settings; it never writes to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import paths

FLATPAK_ID = "io.github.microlift.butter"
WEBSITE = "https://github.com/microlift/butter"


@dataclass(frozen=True)
class Runner:
    name: str
    # "binary" (an official build), "source" (built by Butter) or "custom".
    kind: str
    tag: str | None
    directory: Path
    executable: Path
    flatpak: bool

    def label(self) -> str:
        return f"{self.name} (Butter Flatpak)" if self.flatpak else f"{self.name} (Butter)"


@dataclass(frozen=True)
class Bottle:
    name: str
    prefix: Path
    flatpak: bool

    def label(self) -> str:
        return f"{self.name} (Butter Flatpak bottle)" if self.flatpak else f"{self.name} (Butter bottle)"


def data_dirs() -> list[tuple[Path, bool]]:
    """Butter's data folders, each with whether it is the Flatpak's."""
    return [
        (paths.data_home() / "butter", False),
        (Path.home() / ".var/app" / FLATPAK_ID / "data/butter", True),
    ]


_KEY = re.compile(r"""^([A-Za-z0-9_-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')\s*(?:#.*)?$""")
_ESCAPE = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|(.))")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", "e": "\x1b", '"': '"', "\\": "\\"}


def _unescape(match: re.Match) -> str:
    code = match.group(1) or match.group(2)
    if code:
        return chr(int(code, 16))
    return _ESCAPES.get(match.group(3), match.group(0))


def read_manifest(path: Path) -> dict[str, str]:
    """The top-level string keys of a TOML file Butter wrote.

    Butter writes its manifests with Rust's `toml` crate, which puts plain
    keys before any table. Only those keys are read, which is all nutmeg
    needs, so that nutmeg keeps working on Python 3.10, which has no tomllib.
    """
    try:
        lines = path.read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    found: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            break
        match = _KEY.match(line)
        if match is None:
            continue
        basic, literal = match.group(2), match.group(3)
        found[match.group(1)] = _ESCAPE.sub(_unescape, basic) if basic is not None else literal
    return found


def _version(tag: str | None) -> tuple[int, ...]:
    if not tag:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", tag.split("-", 1)[0]))


def _subdirectories(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    # Butter unpacks into hidden directories and renames them when done.
    return sorted(e for e in entries if e.is_dir() and not e.name.startswith("."))


def runners() -> list[Runner]:
    """Runners whose executable exists, in the order Butter lists them:
    newest release first, custom runners last."""
    found = []
    for data, flatpak in data_dirs():
        for directory in _subdirectories(data / "runners"):
            info = read_manifest(directory / "runner.toml")
            if not info.get("executable"):
                continue
            executable = directory / info["executable"]
            if not executable.is_file():
                continue
            found.append(
                Runner(
                    name=info.get("name") or directory.name,
                    kind=info.get("kind", ""),
                    tag=info.get("tag"),
                    directory=directory,
                    executable=executable,
                    flatpak=flatpak,
                )
            )
    found.sort(
        key=lambda r: (
            r.kind == "custom",
            not _version(r.tag),
            tuple(-n for n in _version(r.tag)),
            r.flatpak,
            r.name,
        )
    )
    return found


def bottles() -> list[Bottle]:
    """Bottles whose rosemary prefix has been created, by name. Like
    `rosemary.prefix_created`, this goes by the prefix's state file, which
    `rosemary prefix create` writes last."""
    found = []
    for data, flatpak in data_dirs():
        for directory in _subdirectories(data / "bottles"):
            info = read_manifest(directory / "bottle.toml")
            if not info.get("name") or not (directory / "prefix/.rosemary/state.yaml").is_file():
                continue
            found.append(Bottle(name=info["name"], prefix=directory / "prefix", flatpak=flatpak))
    found.sort(key=lambda b: (b.name.lower(), b.flatpak))
    return found


def runner_directory(path: Path) -> Path | None:
    """The Butter runner directory `path` is inside, whether or not that
    runner is still installed."""
    for data, _flatpak in data_dirs():
        try:
            relative = path.relative_to(data / "runners")
        except ValueError:
            continue
        if relative.parts:
            return data / "runners" / relative.parts[0]
    return None
