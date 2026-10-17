"""Finding and stopping the Roblox processes nutmeg started, and finding
other nutmeg processes.

A process belongs to an installed build when one of its arguments is a path
inside that build's directory: `rosemary run <versions>/<build>/Roblox.app/...`
for the client, and the same path spelled the way the guest sees it
(`/Volumes/Host/...`) for a program the client started, which rosemary runs by
re-executing itself with the guest's path.
"""

from __future__ import annotations

import os
import re
import signal
import time
from pathlib import Path
from typing import Iterator

from . import paths

GUEST_HOST_MOUNT = "/Volumes/Host"


def _roots(versions: Path) -> list[str]:
    spellings = {str(versions), os.path.realpath(versions)}
    return sorted(spellings | {GUEST_HOST_MOUNT + s for s in spellings})


def _build_of(argument: str, roots: list[str]) -> str | None:
    for root in roots:
        if argument.startswith(root + "/"):
            name = argument[len(root) + 1 :].split("/", 1)[0]
            if name and not name.startswith("."):
                return name
    return None


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
    except OSError:
        return False
    # The state follows the parenthesised command name, which may itself hold
    # parentheses, so it is found from the last one.
    state = stat[stat.rfind(b")") + 2 : stat.rfind(b")") + 3]
    return state not in (b"Z", b"X", b"")


def _commands() -> Iterator[tuple[int, list[str]]]:
    """The pid and arguments of each of this user's processes but this one."""
    me, uid = os.getpid(), os.getuid()
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                arguments = f.read().split(b"\0")
        except OSError:
            continue
        yield pid, [os.fsdecode(raw) for raw in arguments]


def running() -> dict[int, str]:
    """This user's processes running an installed build, as pid -> build."""
    roots = _roots(paths.versions_dir())
    found = {}
    for pid, arguments in _commands():
        for argument in arguments:
            build = _build_of(argument, roots)
            if build is not None:
                if _alive(pid):
                    found[pid] = build
                break
    return found


_NUTMEG_PYZ = re.compile(r"^nutmeg.*\.pyz$")


def _runs_nutmeg(arguments: list[str]) -> bool:
    # `python -m nutmeg` (the menu entries and `nutmeg watch`), the `nutmeg`
    # command (`python <venv>/bin/nutmeg`), or `python nutmeg-<version>.pyz`.
    if any(a == "-m" and b == "nutmeg" for a, b in zip(arguments, arguments[1:])):
        return True
    script = os.path.basename(arguments[1]) if len(arguments) > 1 else ""
    return script == "nutmeg" or _NUTMEG_PYZ.match(script) is not None


def nutmeg_processes() -> list[int]:
    """This user's other processes running nutmeg."""
    return sorted(pid for pid, arguments in _commands() if _runs_nutmeg(arguments) and _alive(pid))


def terminate(pids: list[int], timeout: float = 5.0) -> list[int]:
    """Ask each process to quit, then kill whatever is still running after
    `timeout` seconds. Returns the pids that could not be stopped."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
    deadline = time.monotonic() + timeout
    remaining = [pid for pid in pids if _alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in remaining if _alive(pid)]
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 2.0
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in remaining if _alive(pid)]
    return remaining
