"""Finding, downloading and installing the macOS Roblox client.

Roblox publishes the current macOS build at

    https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer

as `{"version": "0.741.0.7411056", "clientVersionUpload": "version-<hex>"}`,
and the build itself as a zip holding `RobloxPlayer.app`:

    https://setup.rbxcdn.com/mac/<clientVersionUpload>-RobloxPlayer.zip        (x86_64)
    https://setup.rbxcdn.com/mac/arm64/<clientVersionUpload>-RobloxPlayer.zip  (arm64)

Each zip holds a thin executable for its architecture. The bundle is installed
as `Roblox.app`, the name the macOS installer gives it, under
`versions/<clientVersionUpload>-<arch>/`.
"""

from __future__ import annotations

import http.client
import json
import os
import plistlib
import re
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.request
import xml.parsers.expat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import __version__, fsutil, paths
from .errors import Cancelled, NutmegError

USER_AGENT = f"nutmeg/{__version__}"
TIMEOUT = 30

CLIENT_VERSION_URLS = (
    "https://clientsettingscdn.roblox.com/v2/client-version/MacPlayer",
    "https://clientsettings.roblox.com/v2/client-version/MacPlayer",
)
CDN_HOSTS = (
    "https://setup.rbxcdn.com",
    "https://setup-aws.rbxcdn.com",
    "https://setup-ak.rbxcdn.com",
)

BUNDLE_IN_ZIP = "RobloxPlayer.app"
BUNDLE_NAME = "Roblox.app"

# Mach-O `cputype` values.
CPU_TYPES = {"x86_64": 0x01000007, "arm64": 0x0100000C}

_GUID = re.compile(r"^version-[0-9a-f]+$")
_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")

# Room left on the disk beyond what a download or an install needs.
_SPARE_BYTES = 64 * 1024 * 1024
_CHUNK = 1024 * 1024

Progress = Callable[[int, Optional[int]], None]


@dataclass(frozen=True)
class Build:
    guid: str
    """`version-<hex>`, which names the build on Roblox's CDN."""
    version: str
    """The dotted version, for display. Empty when it is not known."""
    arch: str
    """`x86_64` or `arm64`."""

    @property
    def key(self) -> str:
        return f"{self.guid}-{self.arch}"

    @property
    def label(self) -> str:
        return self.version or self.guid

    def directory(self) -> Path:
        return paths.versions_dir() / self.key

    def bundle(self) -> Path:
        return self.directory() / BUNDLE_NAME

    def zip_urls(self) -> list[str]:
        folder = "mac/arm64" if self.arch == "arm64" else "mac"
        return [f"{host}/{folder}/{self.guid}-RobloxPlayer.zip" for host in CDN_HOSTS]


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def _describe(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return str(error) or type(error).__name__


def fetch_latest(arch: str) -> Build:
    """The build Roblox currently ships for macOS."""
    failures = []
    for url in CLIENT_VERSION_URLS:
        try:
            with _open(url) as response:
                data = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except (OSError, ValueError, http.client.HTTPException) as e:
            failures.append(f"{url}: {_describe(e)}")
            continue
        guid = data.get("clientVersionUpload") if isinstance(data, dict) else None
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(guid, str) or not _GUID.match(guid):
            failures.append(f"{url}: unexpected answer {str(data)[:200]!r}")
            continue
        if not isinstance(version, str) or not _VERSION.match(version):
            version = ""
        return Build(guid=guid, version=version, arch=arch)
    raise NutmegError("Could not find out the current Roblox version.\n" + "\n".join(failures))


def _check_space(directory: Path, needed: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < needed + _SPARE_BYTES:
        raise NutmegError(
            f"Not enough disk space in {directory}: {needed / 1e6:.0f} MB is needed, "
            f"{free / 1e6:.0f} MB is free."
        )


def download(build: Build, progress: Progress, cancel: threading.Event) -> Path:
    """Download the build's zip, trying each CDN host in turn."""
    directory = paths.downloads_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{build.key}-RobloxPlayer.zip"
    partial = target.with_name(target.name + ".part")
    failures = []
    for url in build.zip_urls():
        if cancel.is_set():
            raise Cancelled()
        try:
            with _open(url) as response, open(partial, "wb") as out:
                length = response.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else None
                if total is not None:
                    _check_space(directory, total)
                done = 0
                progress(done, total)
                while True:
                    if cancel.is_set():
                        raise Cancelled()
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    progress(done, total)
            if total is not None and done != total:
                raise OSError(f"the download stopped at {done} of {total} bytes")
            _check_zip(partial)
        except (OSError, ValueError, http.client.HTTPException, zipfile.BadZipFile) as e:
            partial.unlink(missing_ok=True)
            failures.append(f"{url}: {_describe(e)}")
            continue
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, target)
        return target
    raise NutmegError(f"Could not download Roblox {build.label}.\n" + "\n".join(failures))


def _check_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        if f"{BUNDLE_IN_ZIP}/Contents/Info.plist" not in archive.namelist():
            raise zipfile.BadZipFile(f"the archive has no {BUNDLE_IN_ZIP}")


def _member_parts(name: str) -> list[str]:
    """The path components of an archive member, refusing any that would land
    outside the directory being extracted into."""
    if name.startswith("/") or "\0" in name:
        raise NutmegError(f"The Roblox archive holds an unsafe path: {name!r}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or parts[0] != BUNDLE_IN_ZIP:
        raise NutmegError(f"The Roblox archive holds an unexpected path: {name!r}")
    return parts


def _check_link_target(name: str, target: str) -> None:
    # A relative target with no `..` can only point at or below the directory
    # holding the link, and that directory is inside the extraction root (by
    # induction over the links before it), so no chain of links escapes.
    parts = target.split("/")
    if not target or target.startswith("/") or "\0" in target or ".." in parts:
        raise NutmegError(f"The Roblox archive holds an unsafe link: {name!r} -> {target!r}")


def _extract_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, root: Path) -> None:
    destination = root.joinpath(*_member_parts(member.filename))
    mode = member.external_attr >> 16 if member.create_system == 3 else 0
    kind = stat.S_IFMT(mode)
    if member.is_dir() or kind == stat.S_IFDIR:
        destination.mkdir(parents=True, exist_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == stat.S_IFLNK:
        try:
            target = archive.read(member).decode("utf-8")
        except UnicodeDecodeError as e:
            raise NutmegError(f"The Roblox archive holds an unreadable link: {member.filename!r}") from e
        _check_link_target(member.filename, target)
        os.symlink(target, destination)
        return
    if kind not in (0, stat.S_IFREG):
        raise NutmegError(f"The Roblox archive holds a special file: {member.filename!r}")
    # The archive's own modes are 0777 and 0666. Only whether a file is
    # executable is kept; the umask decides the rest, as for any new file.
    permissions = 0o777 if mode & 0o111 else 0o666
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(destination, flags, permissions)
    # Reading to the end makes zipfile check the member's CRC-32.
    with os.fdopen(fd, "wb") as out, archive.open(member) as source:
        shutil.copyfileobj(source, out, _CHUNK)


def _plist_file_name(bundle: Path, key: str, what: str) -> str:
    """A file name the bundle's Info.plist gives under `key`."""
    info = bundle / "Contents" / "Info.plist"
    try:
        with open(info, "rb") as f:
            plist = plistlib.load(f)
    except (OSError, ValueError, xml.parsers.expat.ExpatError) as e:
        raise NutmegError(f"Could not read {info}: {e}") from e
    name = plist.get(key) if isinstance(plist, dict) else None
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        raise NutmegError(f"{info} does not name {what}")
    return name


def executable(bundle: Path) -> Path:
    """The main executable an app bundle's Info.plist names."""
    return bundle / "Contents" / "MacOS" / _plist_file_name(bundle, "CFBundleExecutable", "an executable")


def icon_file(bundle: Path) -> Path:
    """The .icns file an app bundle's Info.plist names as its icon."""
    name = _plist_file_name(bundle, "CFBundleIconFile", "an icon")
    if not name.endswith(".icns"):
        name += ".icns"
    return bundle / "Contents" / "Resources" / name


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def icns_pngs(data: bytes) -> dict[int, bytes]:
    """The square PNG images in an .icns file, by width. The file is a list of
    (4-byte type, 4-byte big-endian length that counts these 8 bytes, data)
    after its own 8-byte header. The other formats it can hold (raw ARGB,
    JPEG 2000 and the older packed ones) are left out."""
    if data[:4] != b"icns" or len(data) < 8:
        return {}
    end = min(len(data), int.from_bytes(data[4:8], "big"))
    found: dict[int, bytes] = {}
    offset = 8
    while offset + 8 <= end:
        length = int.from_bytes(data[offset + 4 : offset + 8], "big")
        if length < 8 or offset + length > end:
            break
        body = data[offset + 8 : offset + length]
        offset += length
        # The PNG signature, then the IHDR chunk's length, type, width and height.
        if len(body) < 24 or body[:8] != _PNG_SIGNATURE or body[12:16] != b"IHDR":
            continue
        width = int.from_bytes(body[16:20], "big")
        if width == int.from_bytes(body[20:24], "big") and width not in found:
            found[width] = body
    return found


def macho_architectures(path: Path) -> set[str]:
    """The architectures a Mach-O file has code for, by the names in
    `CPU_TYPES`. Empty for anything that is not a 64-bit Mach-O or a fat file."""
    names = {cpu: name for name, cpu in CPU_TYPES.items()}
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return set()
        if int.from_bytes(head[:4], "little") == 0xFEEDFACF:
            cpu = int.from_bytes(head[4:8], "little")
            return {names[cpu]} if cpu in names else set()
        magic = int.from_bytes(head[:4], "big")
        if magic not in (0xCAFEBABE, 0xCAFEBABF):
            return set()
        count = int.from_bytes(head[4:8], "big")
        # A Java class file shares the magic, and has its version (45 or more)
        # where a fat header has its slice count.
        if count > 16:
            return set()
        size = 20 if magic == 0xCAFEBABE else 32
        table = f.read(count * size)
    found = set()
    for i in range(min(count, len(table) // size)):
        cpu = int.from_bytes(table[i * size : i * size + 4], "big")
        if cpu in names:
            found.add(names[cpu])
    return found


def _verify_bundle(bundle: Path, arch: str) -> Path:
    program = executable(bundle)
    if not program.is_file() or not os.access(program, os.X_OK):
        raise NutmegError(f"{program} is missing or not executable")
    found = macho_architectures(program)
    if arch not in found:
        have = ", ".join(sorted(found)) or "no recognised architecture"
        raise NutmegError(f"{program} was expected to be {arch}, but has {have}")
    return program


def installed_executable(build: Build) -> Path | None:
    """The build's executable if it is installed and intact, else None."""
    bundle = build.bundle()
    if not bundle.is_dir():
        return None
    try:
        return _verify_bundle(bundle, build.arch)
    except (NutmegError, OSError):
        return None


def install(zip_path: Path, build: Build, progress: Progress, cancel: threading.Event) -> Path:
    """Unpack a downloaded zip into the build's directory and return the
    executable. The directory appears complete or not at all."""
    versions = paths.versions_dir()
    versions.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".tmp-{build.key}-", dir=versions))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            _check_space(versions, sum(m.file_size for m in members))
            for index, member in enumerate(members, 1):
                if cancel.is_set():
                    raise Cancelled()
                _extract_member(archive, member, staging)
                progress(index, len(members))
        unpacked = staging / BUNDLE_IN_ZIP
        _verify_bundle(unpacked, build.arch)
        os.rename(unpacked, staging / BUNDLE_NAME)
        final = build.directory()
        if final.exists():
            # A build that was installed but no longer verifies.
            shutil.rmtree(final)
        os.rename(staging, final)
    except (zipfile.BadZipFile, zlib.error, EOFError) as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise NutmegError(f"The downloaded Roblox archive is damaged: {e}") from e
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return _verify_bundle(build.bundle(), build.arch)


def load_installed() -> Build | None:
    """The build nutmeg last installed, if the record of it is readable."""
    try:
        data = json.loads(paths.installed_file().read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    guid, version, arch = data.get("guid"), data.get("version", ""), data.get("arch")
    if not isinstance(guid, str) or not _GUID.match(guid) or arch not in CPU_TYPES:
        return None
    if not isinstance(version, str) or (version and not _VERSION.match(version)):
        version = ""
    return Build(guid=guid, version=version, arch=arch)


def save_installed(build: Build) -> None:
    fsutil.write_json(
        paths.installed_file(),
        {"guid": build.guid, "version": build.version, "arch": build.arch},
    )


def forget_installed() -> None:
    paths.installed_file().unlink(missing_ok=True)


def fast_flags_file(bundle: Path) -> Path:
    return bundle / "Contents" / "MacOS" / "ClientSettings" / "ClientAppSettings.json"


def write_fast_flags(bundle: Path, flags: dict) -> None:
    """Write the user's FastFlags where the client reads them, or remove the
    file when there are none."""
    path = fast_flags_file(bundle)
    if flags:
        fsutil.write_json(path, flags)
    else:
        path.unlink(missing_ok=True)


def installed_keys() -> list[str]:
    """The builds present under `versions/`, by directory name."""
    try:
        entries = list(os.scandir(paths.versions_dir()))
    except FileNotFoundError:
        return []
    return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith("."))


def remove_leftovers() -> None:
    """Remove what an interrupted download or install left behind. Only safe
    while holding the install lock."""
    try:
        entries = list(os.scandir(paths.versions_dir()))
    except FileNotFoundError:
        entries = []
    for entry in entries:
        if entry.name.startswith(".tmp-") and entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
    try:
        downloads = list(os.scandir(paths.downloads_dir()))
    except FileNotFoundError:
        downloads = []
    for entry in downloads:
        if entry.name.endswith((".zip", ".zip.part")) and entry.is_file(follow_symlinks=False):
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                pass


def remove_versions(keep: set[str], in_use: set[str]) -> list[str]:
    """Remove installed builds other than `keep` and those in use. Returns the
    names removed. Only safe while holding the install lock."""
    removed = []
    for name in installed_keys():
        if name in keep or name in in_use:
            continue
        shutil.rmtree(paths.versions_dir() / name, ignore_errors=True)
        removed.append(name)
    return removed
