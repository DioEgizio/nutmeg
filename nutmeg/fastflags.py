"""FastFlags as the flag editor sees them: a name, a kind and a value.

The flags are stored in the settings as one JSON object and written unchanged
to ClientAppSettings.json, so every JSON value has to survive the editor. The
kinds are the three plain types flags take, and `json` for anything else (a
fractional number, null, a list or an object), edited as JSON text.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from .config import ConfigError

KINDS = ("bool", "int", "string", "json")
KIND_LABELS = {"bool": "Boolean", "int": "Integer", "string": "String", "json": "JSON"}

_INTEGER = re.compile(r"^-?[0-9]+$")

# Flag name prefixes, as the Roblox client's own strings spell them, and the
# kind of value each takes.
_PREFIX_KINDS = (
    ("FFlag", "bool"),
    ("DFFlag", "bool"),
    ("SFFlag", "bool"),
    ("FInt", "int"),
    ("DFInt", "int"),
    ("FString", "string"),
    ("DFString", "string"),
)


def kind_of(value: object) -> str:
    """The kind a stored value is shown as."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "string"
    return "json"


def kind_for_name(name: str) -> str | None:
    """The kind a flag's name prefix says it takes, or None."""
    name = name.strip()
    for prefix, kind in _PREFIX_KINDS:
        if name.startswith(prefix) and len(name) > len(prefix):
            return kind
    return None


def text_of(value: object) -> str:
    """The text a stored value is edited as, under its own kind."""
    kind = kind_of(value)
    if kind == "string":
        return value  # type: ignore[return-value]
    if kind == "bool":
        return "true" if value else "false"
    if kind == "int":
        return str(value)
    return json.dumps(value)


def value_of(kind: str, text: str, active: bool) -> object:
    """The value a row holds: `active` for a Boolean, `text` read as `kind`
    otherwise. Raises ValueError with the rest of a sentence about the flag."""
    if kind == "bool":
        return active
    if kind == "int":
        if not _INTEGER.match(text.strip()):
            raise ValueError("needs a whole number")
        return int(text.strip())
    if kind == "string":
        return text
    if kind == "json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"is not valid JSON ({e.msg})") from e
    raise ValueError(f"has an unknown type {kind!r}")


def collect(rows: Iterable[tuple[str, str, str, bool]]) -> dict:
    """The flags object for (name, kind, text, active) rows, in their order.
    Raises ConfigError naming the first row that cannot be used."""
    flags: dict = {}
    for number, (name, kind, text, active) in enumerate(rows, 1):
        name = name.strip()
        if not name:
            raise ConfigError(f"FastFlags: the flag in row {number} has no name")
        if name in flags:
            raise ConfigError(f"FastFlags: {name} is listed twice")
        try:
            flags[name] = value_of(kind, text, active)
        except ValueError as e:
            raise ConfigError(f"FastFlags: {name} {e}") from e
    return flags


def parse_object(text: str) -> dict:
    """Flags pasted or loaded as a JSON object; empty text is no flags."""
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Not valid JSON: {e}") from e
    if not isinstance(value, dict):
        raise ConfigError('The flags must be a JSON object, such as {"FlagName": true}')
    return value
