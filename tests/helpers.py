"""Test fixtures: a scratch XDG home, fake Roblox archives, a local web server
standing in for Roblox's endpoints, and a fake rosemary."""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import os
import plistlib
import re
import shutil
import socket
import stat
import struct
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipapp
import zipfile
import zlib
from pathlib import Path

from unittest import mock

from nutmeg import paths, presence, roblox

ROOT = Path(__file__).resolve().parent.parent


def macho(arch: str) -> bytes:
    return struct.pack("<II", 0xFEEDFACF, roblox.CPU_TYPES[arch]) + b"\0" * 56


def fat(*archs: str) -> bytes:
    head = struct.pack(">II", 0xCAFEBABE, len(archs))
    for arch in archs:
        head += struct.pack(">iiIII", roblox.CPU_TYPES[arch], 3, 4096, 64, 12)
    return head


def _add(archive: zipfile.ZipFile, name: str, data: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    archive.writestr(info, data)


def png(width: int, height: int | None = None, rgba: bytes = b"\xe2\x23\x1a\xff") -> bytes:
    """A PNG of one colour."""
    height = width if height is None else height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = (b"\0" + rgba * width) * height
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def icns(entries: list[tuple[bytes, bytes]]) -> bytes:
    """An .icns file holding (type, data) entries."""
    body = b"".join(kind + struct.pack(">I", len(data) + 8) + data for kind, data in entries)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


# Shaped like the AppIcon.icns in Roblox 0.741: PNGs at 256 (128@2x), 32
# (16@2x) and 128 pixels, and a 16-pixel image in raw ARGB.
ROBLOX_ICONS = {256: png(256), 32: png(32), 128: png(128)}
ROBLOX_ICNS = icns(
    [
        (b"ic13", ROBLOX_ICONS[256]),
        (b"ic11", ROBLOX_ICONS[32]),
        (b"ic04", b"ARGB" + b"\xff" * 64),
        (b"ic07", ROBLOX_ICONS[128]),
    ]
)


def roblox_zip(
    arch: str = "x86_64", extra: list[tuple[str, bytes, int]] | None = None, icon: bool = True
) -> bytes:
    """A zip shaped like Roblox's: RobloxPlayer.app with 0777 and 0666 modes,
    and with the AppIcon.icns its Info.plist names unless not `icon`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _add(archive, "RobloxPlayer.app/", b"", 0o40755)
        _add(archive, "RobloxPlayer.app/Contents/", b"", 0o40755)
        info = {"CFBundleExecutable": "RobloxPlayer", "CFBundleIdentifier": "com.roblox.RobloxPlayer"}
        if icon:
            info["CFBundleIconFile"] = "AppIcon"
        _add(archive, "RobloxPlayer.app/Contents/Info.plist", plistlib.dumps(info), 0o100666)
        _add(archive, "RobloxPlayer.app/Contents/MacOS/RobloxPlayer", macho(arch), 0o100777)
        _add(archive, "RobloxPlayer.app/Contents/Resources/content.txt", b"content" * 100, 0o100666)
        if icon:
            _add(archive, "RobloxPlayer.app/Contents/Resources/AppIcon.icns", ROBLOX_ICNS, 0o100666)
        for name, data, mode in extra or []:
            _add(archive, name, data, mode)
    return buffer.getvalue()


def nutmeg_pyz(version: str) -> bytes:
    """This checkout's nutmeg as a zipapp stamped with `version`, made the
    way scripts/build_nightly.py makes one."""
    with tempfile.TemporaryDirectory(prefix="nutmeg-pyz-") as scratch:
        staging = Path(scratch) / "app"
        shutil.copytree(ROOT / "nutmeg", staging / "nutmeg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        init = staging / "nutmeg/__init__.py"
        init.write_text(re.sub(r'__version__ = "[^"]*"', f'__version__ = "{version}"', init.read_text()))
        (staging / "__main__.py").write_text("import sys\n\nfrom nutmeg.cli import main\n\nsys.exit(main())\n")
        target = Path(scratch) / "nutmeg.pyz"
        zipapp.create_archive(staging, target, interpreter="/usr/bin/env python3", compressed=True)
        return target.read_bytes()


def fake_wheel(version: str) -> bytes:
    """A wheel of a stand-in nutmeg that only answers --version."""
    info = f"nutmeg-{version}.dist-info"
    files = {
        "nutmeg/__init__.py": f'__version__ = "{version}"\n',
        "nutmeg/__main__.py": "import sys\nfrom nutmeg import __version__\nprint('nutmeg', __version__)\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: nutmeg\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: nutmeg-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = ""
    for name, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record += f"{name},sha256={digest},{len(text.encode())}\n"
    files[f"{info}/RECORD"] = record + f"{info}/RECORD,,\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class Server:
    """Serves `routes` (path -> bytes, or path -> int status). A route whose
    value is a (bytes, claimed_length) pair claims a longer body than it sends."""

    def __init__(self) -> None:
        self.routes: dict = {}
        self.requests: list[str] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.requests.append(self.path)
                route = server.routes.get(self.path, 404)
                if isinstance(route, int):
                    self.send_response(route)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body, length = route if isinstance(route, tuple) else (route, len(route))
                self.send_response(200)
                self.send_header("Content-Length", str(length))
                self.end_headers()
                self.wfile.write(body)
                if length != len(body):
                    self.close_connection = True

            def log_message(self, *args) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def publish_nutmeg(
        self, tag: str, files: dict[str, bytes], sums: str | None = None, prerelease: bool = True
    ) -> None:
        """Add a nutmeg release to `/releases`, shaped like GitHub's answer,
        with its files under `/download/<tag>/` and a SHA256SUMS of them
        (or `sums`). Releases added later are listed first, as on GitHub. A
        release that is not a pre-release also becomes `/releases/latest`."""
        if sums is None:
            sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items())
        files = dict(files, SHA256SUMS=sums.encode())
        for name, data in files.items():
            self.routes[f"/download/{tag}/{name}"] = data
        assets = [{"name": name, "browser_download_url": f"{self.url}/download/{tag}/{name}"} for name in files]
        releases = json.loads(self.routes.get("/releases", b"[]"))
        release = {"tag_name": tag, "draft": False, "prerelease": prerelease, "assets": assets}
        releases.insert(0, release)
        self.routes["/releases"] = json.dumps(releases).encode()
        if not prerelease:
            self.routes["/releases/latest"] = json.dumps(release).encode()

    def publish(self, guid: str, version: str, archives: dict[str, bytes], hosts: list[str]) -> None:
        self.routes["/client-version"] = json.dumps(
            {"version": version, "clientVersionUpload": guid, "bootstrapperVersion": ""}
        ).encode()
        for host in hosts:
            for arch, data in archives.items():
                folder = "mac/arm64" if arch == "arm64" else "mac"
                self.routes[f"/{host}/{folder}/{guid}-RobloxPlayer.zip"] = data


FAKE_ROSEMARY = textwrap.dedent(
    """\
    #!{python}
    # Stands in for rosemary: `prefix create` makes the state file nutmeg looks
    # for, and `run` records how it was started, then waits or fails as asked.
    import json, os, sys, time
    args = sys.argv[1:]
    record = os.environ.get("FAKE_ROSEMARY_RECORD")
    if args[:2] == ["prefix", "create"]:
        if os.environ.get("FAKE_ROSEMARY_PREFIX_FAIL"):
            print("rosemary: prefix: could not download the base", flush=True)
            sys.exit(1)
        directory = args[2]
        os.makedirs(os.path.join(directory, "root", "Applications"), exist_ok=True)
        os.makedirs(os.path.join(directory, ".rosemary"), exist_ok=True)
        lines = ["version: 1", "created_with:", '  runtime_version: "0.1.0"']
        if "--arch" in args:
            lines += ["base:", '  project: "minidarwin"', '  arch: "%s"' % args[args.index("--arch") + 1]]
        with open(os.path.join(directory, ".rosemary", "state.yaml"), "w") as f:
            f.write("\\n".join(lines) + "\\n")
        with open(record + ".prefix", "w") as f:
            json.dump({{"argv": args, "setup": os.environ.get("ROSEMARY_PREFIX_SETUP")}}, f)
        print("created " + directory, flush=True)
        sys.exit(0)
    if args[:2] == ["server", "--stop"]:
        with open(record + ".server", "a") as f:
            f.write(os.environ.get("ROSEMARY_PREFIX", "") + "\\n")
        print("rosemary server: no server at /run/user/rosemary/server-host", flush=True)
        sys.exit(1)
    if args[:2] == ["prefix", "repair"]:
        print("nothing to repair", flush=True)
        sys.exit(0)
    if args[:1] == ["run"]:
        env = {{k: v for k, v in os.environ.items() if k.startswith("ROSEMARY_")}}
        with open(record, "w") as f:
            json.dump({{"argv": args, "env": env, "cwd": os.getcwd()}}, f)
        print("guest output", flush=True)
        code = int(os.environ.get("FAKE_ROSEMARY_EXIT", "-1"))
        if code >= 0:
            sys.exit(code)
        time.sleep(60)
        sys.exit(0)
    sys.exit(2)
    """
)


class FakeDiscord:
    """Listens where the Discord app does, in XDG_RUNTIME_DIR, answers the
    handshake with READY and each SET_ACTIVITY with its nonce, and records
    the activities."""

    def __init__(self, runtime_dir: Path) -> None:
        self.path = runtime_dir / "discord-ipc-0"
        self.activities: list = []
        self.client_ids: list[str] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._talk, args=(connection,), daemon=True).start()

    @staticmethod
    def _read(connection: socket.socket):
        header = b""
        while len(header) < 8:
            chunk = connection.recv(8 - len(header))
            if not chunk:
                return None
            header += chunk
        op, length = struct.unpack("<II", header)
        body = b""
        while len(body) < length:
            body += connection.recv(length - len(body))
        return op, json.loads(body)

    @staticmethod
    def _write(connection: socket.socket, op: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        connection.sendall(struct.pack("<II", op, len(data)) + data)

    def _talk(self, connection: socket.socket) -> None:
        with connection:
            while True:
                try:
                    message = self._read(connection)
                except OSError:
                    return
                if message is None:
                    return
                op, payload = message
                if op == 0:
                    self.client_ids.append(payload["client_id"])
                    self._write(connection, 1, {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1}})
                elif payload.get("cmd") == "SET_ACTIVITY":
                    self.activities.append(payload["args"]["activity"])
                    self._write(connection, 1, {"cmd": "SET_ACTIVITY", "evt": None, "nonce": payload["nonce"]})

    def close(self) -> None:
        self._server.close()


class Sandbox(unittest.TestCase):
    """Points every XDG directory and HOME at a scratch directory."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="nutmeg-test-"))
        self._saved_env = dict(os.environ)
        os.environ["HOME"] = str(self.root / "home")
        for variable, name in (
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_DATA_HOME", "data"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_STATE_HOME", "state"),
        ):
            os.environ[variable] = str(self.root / name)
        os.environ["XDG_RUNTIME_DIR"] = str(self.root / "run")
        (self.root / "run").mkdir()
        for name in ("ROSEMARY_PREFIX", "ROSEMARY_DEAD_FLAGS", "FAKE_ROSEMARY_EXIT", "FAKE_ROSEMARY_PREFIX_FAIL"):
            os.environ.pop(name, None)
        # Not in an AppImage, also when scripts/build_appimage.py runs the
        # tests on the Python it bundles.
        for name in [paths.APPIMAGE_VARS, "APPIMAGE", "APPDIR", "ARGV0", "OWD"]:
            os.environ.pop(name, None)
        for name in [n for n in os.environ if n.startswith("NUTMEG_HOST_")]:
            del os.environ[name]
        (self.root / "home").mkdir()
        # A launch would otherwise start a real `nutmeg watch`.
        patcher = mock.patch.object(presence, "start_helper")
        self.presence_helper = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.root, ignore_errors=True)

    def fake_rosemary(self) -> Path:
        path = self.root / "bin" / "rosemary"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE_ROSEMARY.format(python=sys.executable))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        os.environ["FAKE_ROSEMARY_RECORD"] = str(self.root / "record.json")
        return path

    def butter_runner(self, name: str, toml: str, flatpak: bool = False) -> Path:
        """A runner directory shaped like Butter's, with an executable
        `bin/rosemary` and `toml` as its runner.toml. Returns the executable."""
        data = self.root / "home/.var/app/io.github.microlift.butter/data" if flatpak else self.root / "data"
        directory = data / "butter/runners" / name
        (directory / "bin").mkdir(parents=True)
        (directory / "runner.toml").write_text(toml)
        executable = directory / "bin/rosemary"
        executable.write_text(FAKE_ROSEMARY.format(python=sys.executable))
        executable.chmod(0o755)
        return executable

    def butter_bottle(self, name: str, toml: str, created: bool = True) -> Path:
        """A bottle directory shaped like Butter's. Returns its prefix."""
        directory = self.root / "data/butter/bottles" / name
        directory.mkdir(parents=True)
        (directory / "bottle.toml").write_text(toml)
        if created:
            (directory / "prefix/.rosemary").mkdir(parents=True)
            (directory / "prefix/.rosemary/state.yaml").write_text("version: 1\n")
        return directory / "prefix"

    def record(self, timeout: float = 10.0) -> dict:
        """What the fake rosemary recorded about its `run`. The process writes
        it a moment after starting, so this waits for it."""
        path = self.root / "record.json"
        deadline = time.monotonic() + timeout
        while True:
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def wait_for_text(self, path: Path, text: str, timeout: float = 10.0) -> str:
        """The contents of `path` once they include `text`."""
        deadline = time.monotonic() + timeout
        while True:
            contents = path.read_text()
            if text in contents or time.monotonic() > deadline:
                return contents
            time.sleep(0.05)
