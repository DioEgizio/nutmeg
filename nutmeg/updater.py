"""Updating nutmeg itself from its GitHub releases.

nutmeg publishes two kinds of release: releases, versioned X.Y.Z, and nightly
pre-releases, versioned X.Y.Z.devN. A copy follows the kind it is: a release
updates to newer releases only, and a nightly build to newer nightly builds
only. A release is found as GitHub's latest release, so that however many
nightly builds came after it, it is one request.

Each release has a zipapp `nutmeg-<version>.pyz`, a wheel
`nutmeg-<version>-py3-none-any.whl`, an AppImage
`nutmeg-<version>-<machine>.AppImage` for each machine and SHA256SUMS, among
other files. A copy of nutmeg updates itself when it is one of these:

- a .pyz file or an AppImage nutmeg can write to, which is replaced in
  place, so that the menu entries and anything else pointing at it run the
  new version;
- a wheel installed in a virtual environment, such as pipx's, where the new
  wheel is installed with that environment's pip.

A source checkout is left to git, the Flatpak to a newer .flatpak, and any
other installation to whatever installed it.

The launcher checks for a newer version at the start of a launch, installs
it, and starts nutmeg again with the same request, so that the launch runs on
the new code. Nothing is installed while another nutmeg process runs:
zipimport reads a .pyz file's table of contents once, and each module from
the file when it is first imported, so a process still running from a
replaced .pyz could not import from it. The update waits for a launch with
no other nutmeg running.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import re
import stat
import subprocess
import sys
import sysconfig
import tempfile
import urllib.error
import urllib.request
import zipimport
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Optional

from . import __version__, desktop, paths
from .errors import NutmegError
from .roblox import USER_AGENT

RELEASES_URL = "https://api.github.com/repos/frolleks/nutmeg/releases?per_page=10"
# The newest release that is not a pre-release (404 while there is none).
LATEST_URL = "https://api.github.com/repos/frolleks/nutmeg/releases/latest"
# Where a release's files must be. Others in the list are ignored.
DOWNLOADS = "https://github.com/frolleks/nutmeg/releases/download/"
TIMEOUT = 10
# The most a downloaded file may be. A release's files are well under 1 MB,
# but for the AppImages, which are about 40 MB.
MAX_BYTES = 20 * 1024 * 1024
MAX_APPIMAGE_BYTES = 300 * 1024 * 1024
# Set for a nutmeg started again after an update, to the version it replaced.
UPDATED_FROM = "NUTMEG_UPDATED_FROM"

# The versions nutmeg publishes: X.Y.Z, and X.Y.Z.devN builds before it.
_VERSION = re.compile(r"^([0-9]+(?:\.[0-9]+)*)(?:\.dev([0-9]+))?$")
_FILE_NAMES = {
    "pyz": re.compile(r"^nutmeg-(.+)\.pyz$"),
    "wheel": re.compile(r"^nutmeg-(.+)-py3-none-any\.whl$"),
    "appimage": re.compile(rf"^nutmeg-(.+)-{re.escape(platform.machine())}\.AppImage$"),
}
_CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")


def version_key(version: str) -> Optional[tuple]:
    """A key that orders versions as PEP 440 does, for the versions nutmeg
    publishes. None for any other version."""
    match = _VERSION.match(version)
    if match is None:
        return None
    release = [int(part) for part in match.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    dev = match.group(2)
    return (tuple(release), dev is None, int(dev or 0))


def is_release(version: str) -> bool:
    """Whether `version` is a release rather than a nightly build."""
    key = version_key(version)
    return key is not None and key[1]


@dataclass(frozen=True)
class Installation:
    kind: Optional[str]
    """`pyz`, `appimage` or `wheel` for a copy nutmeg updates, None for one it
    does not."""
    path: Path
    description: str
    """What this copy is and how it is updated, for the settings window."""
    python: str = sys.executable
    """The interpreter that installs and runs a wheel."""


def installation() -> Installation:
    """How the running copy of nutmeg is installed."""
    if paths.in_flatpak():
        return Installation(
            None,
            Path(__file__).resolve().parent,
            "This copy is the Flatpak, which updates when you install a newer .flatpak "
            "from nutmeg's Releases page.",
        )
    image = paths.appimage()
    if image is not None:
        image = image.resolve()
        if os.access(image, os.W_OK) and os.access(image.parent, os.W_OK):
            return Installation("appimage", image, f"This copy is {image}. An update replaces it.")
        return Installation(
            None, image, f"This copy is {image}, which nutmeg cannot change, so it is not updated."
        )
    loader = __spec__.loader if __spec__ is not None else None
    if isinstance(loader, zipimport.zipimporter):
        archive = Path(loader.archive).resolve()
        if os.access(archive, os.W_OK) and os.access(archive.parent, os.W_OK):
            return Installation("pyz", archive, f"This copy is {archive}. An update replaces it.")
        return Installation(
            None, archive, f"This copy is {archive}, which nutmeg cannot change, so it is not updated."
        )
    package = Path(__file__).resolve().parent
    root = package.parent
    if (root / ".git").exists():
        return Installation(
            None, root, f"This copy runs from the source checkout at {root}, so it is not updated. Use git pull."
        )
    purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    if sys.prefix != sys.base_prefix and root == purelib and os.access(root, os.W_OK):
        return Installation(
            "wheel",
            Path(sys.prefix),
            f"This copy is installed in {sys.prefix}. An update installs the new wheel there with pip.",
        )
    return Installation(
        None,
        package,
        f"This copy is installed at {package}, which nutmeg does not update. "
        "Update it the way you installed it.",
    )


@dataclass(frozen=True)
class Release:
    version: str
    name: str
    """The file for the installation: a .pyz or a wheel."""
    url: str
    checksums: str
    """The URL of the release's SHA256SUMS."""


def _describe(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return str(error) or type(error).__name__


def _fetch(url: str, limit: int, missing: Optional[bytes] = None) -> bytes:
    """The body at `url`, or `missing` when it is given and the answer is 404."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404 and missing is not None:
            e.close()
            return missing
        raise NutmegError(f"Could not download {url}: {_describe(e)}") from e
    except (OSError, http.client.HTTPException) as e:
        raise NutmegError(f"Could not download {url}: {_describe(e)}") from e
    if len(data) > limit:
        raise NutmegError(f"{url} is larger than nutmeg expects")
    return data


def _published(nightly: bool) -> list:
    """GitHub's answer for the newest nightly builds, or for the latest
    release, as a list of releases."""
    try:
        if nightly:
            data = _fetch(RELEASES_URL, MAX_BYTES)
        else:
            data = _fetch(LATEST_URL, MAX_BYTES, missing=b"null")
        answer = json.loads(data.decode("utf-8"))
    except ValueError as e:
        raise NutmegError(f"Could not read GitHub's list of nutmeg releases: {e}") from e
    if nightly:
        return answer if isinstance(answer, list) else []
    return [answer] if answer is not None else []


def newest(kind: str, nightly: bool) -> Optional[Release]:
    """The newest nightly build (when `nightly`) or release that has a file
    for `kind`, or None."""
    pattern = _FILE_NAMES[kind]
    best: Optional[Release] = None
    for release in _published(nightly):
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if bool(release.get("prerelease")) != nightly:
            continue
        files = {}
        for asset in release.get("assets") or []:
            name = asset.get("name") if isinstance(asset, dict) else None
            url = asset.get("browser_download_url") if isinstance(asset, dict) else None
            if isinstance(name, str) and isinstance(url, str) and url.startswith(DOWNLOADS):
                files[name] = url
        checksums = files.get("SHA256SUMS")
        if checksums is None:
            continue
        for name, url in files.items():
            match = pattern.match(name)
            key = version_key(match.group(1)) if match else None
            if key is not None and (best is None or key > version_key(best.version)):
                best = Release(match.group(1), name, url, checksums)
    return best


def newer(installation: Installation, current: str = __version__) -> Optional[Release]:
    """A published version newer than `current`, of the same kind (release or
    nightly build), for this installation, or None."""
    have = version_key(current)
    if installation.kind is None or have is None:
        return None
    release = newest(installation.kind, nightly=not is_release(current))
    if release is None or version_key(release.version) <= have:
        return None
    return release


def _clean_env() -> dict[str, str]:
    # A PYTHONPATH naming the old copy would put it ahead of the new one.
    env = paths.host_env()
    env.pop("PYTHONPATH", None)
    return env


def _check_version(command: list[str], version: str) -> None:
    """Run `command --version` and check that it is the expected nutmeg."""
    try:
        result = subprocess.run(
            command + ["--version"],
            capture_output=True,
            text=True,
            env=_clean_env(),
            cwd="/",
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NutmegError(f"Could not run nutmeg {version}: {e}") from e
    printed = result.stdout.strip()
    if result.returncode != 0 or printed != f"nutmeg {version}":
        detail = result.stderr.strip().splitlines()[-1:] or [f"exit status {result.returncode}"]
        raise NutmegError(f"nutmeg {version} printed {printed!r} for its version ({detail[0]})")


def _replace_file(path: Path, data: bytes, version: str, run: list[str]) -> None:
    """Write `data` next to `path`, check that `run` followed by it is nutmeg
    `version`, and put it in place of `path`. A running AppImage keeps
    reading the file it was started from, which the rename leaves alone."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=path.suffix, dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(staged, mode)
        _check_version(run + [str(staged)], version)
        os.replace(staged, path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _install_wheel(python: str, data: bytes, name: str, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="nutmeg-update-") as folder:
        wheel = Path(folder) / name
        wheel.write_bytes(data)
        # Only this file: no index, and no dependencies (nutmeg has none).
        command = [python, "-m", "pip", "install", "--no-deps", "--no-index"]
        command += ["--disable-pip-version-check", "--no-input", "--quiet", str(wheel)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=_clean_env(),
                cwd="/",
                stdin=subprocess.DEVNULL,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise NutmegError(f"Could not run pip: {e}") from e
        if result.returncode != 0:
            said = (result.stderr or result.stdout).strip().splitlines() or [f"exit status {result.returncode}"]
            raise NutmegError(f"pip could not install {name}: {said[-1]}")
    _check_version([python, "-m", "nutmeg"], version)


def install(installation: Installation, release: Release) -> None:
    """Download `release`, check it against SHA256SUMS, and install it."""
    data = _fetch(release.url, MAX_APPIMAGE_BYTES if installation.kind == "appimage" else MAX_BYTES)
    sums = _fetch(release.checksums, 1024 * 1024).decode("utf-8", "replace")
    expected = None
    for line in sums.splitlines():
        match = _CHECKSUM_LINE.match(line.rstrip("\r"))
        if match and match.group(2) == release.name:
            expected = match.group(1)
    if expected is None:
        raise NutmegError(f"The SHA256SUMS of nutmeg {release.version} does not list {release.name}")
    if hashlib.sha256(data).hexdigest() != expected:
        raise NutmegError(f"{release.name} does not match its checksum in SHA256SUMS")
    if installation.kind == "pyz":
        _replace_file(installation.path, data, release.version, [sys.executable])
    elif installation.kind == "appimage":
        _replace_file(installation.path, data, release.version, [])
    elif installation.kind == "wheel":
        _install_wheel(installation.python, data, release.name, release.version)
    else:
        raise NutmegError(installation.description)


def restart(arguments: list[str], previous: str) -> NoReturn:
    """Replace this process with the installed nutmeg, run with `arguments`."""
    command = desktop.nutmeg_command() + arguments
    env = paths.host_env()
    env[UPDATED_FROM] = previous
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(command[0], command, env)
