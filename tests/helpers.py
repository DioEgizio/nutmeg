"""Test fixtures: a scratch XDG home, fake Roblox archives, a local web server
standing in for Roblox's endpoints, and a fake rosetta3."""

from __future__ import annotations

import http.server
import io
import json
import os
import plistlib
import shutil
import stat
import struct
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from pathlib import Path

from rostrap import roblox


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


def roblox_zip(arch: str = "x86_64", extra: list[tuple[str, bytes, int]] | None = None) -> bytes:
    """A zip shaped like Roblox's: RobloxPlayer.app with 0777 and 0666 modes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _add(archive, "RobloxPlayer.app/", b"", 0o40755)
        _add(archive, "RobloxPlayer.app/Contents/", b"", 0o40755)
        plist = plistlib.dumps({"CFBundleExecutable": "RobloxPlayer", "CFBundleIdentifier": "com.roblox.RobloxPlayer"})
        _add(archive, "RobloxPlayer.app/Contents/Info.plist", plist, 0o100666)
        _add(archive, "RobloxPlayer.app/Contents/MacOS/RobloxPlayer", macho(arch), 0o100777)
        _add(archive, "RobloxPlayer.app/Contents/Resources/content.txt", b"content" * 100, 0o100666)
        for name, data, mode in extra or []:
            _add(archive, name, data, mode)
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

    def publish(self, guid: str, version: str, archives: dict[str, bytes], hosts: list[str]) -> None:
        self.routes["/client-version"] = json.dumps(
            {"version": version, "clientVersionUpload": guid, "bootstrapperVersion": ""}
        ).encode()
        for host in hosts:
            for arch, data in archives.items():
                folder = "mac/arm64" if arch == "arm64" else "mac"
                self.routes[f"/{host}/{folder}/{guid}-RobloxPlayer.zip"] = data


FAKE_ROSETTA3 = textwrap.dedent(
    """\
    #!{python}
    # Stands in for rosetta3: `prefix create` makes the state file rostrap looks
    # for, and `run` records how it was started, then waits or fails as asked.
    import json, os, sys, time
    args = sys.argv[1:]
    record = os.environ.get("FAKE_ROSETTA3_RECORD")
    if args[:2] == ["prefix", "create"]:
        if os.environ.get("FAKE_ROSETTA3_PREFIX_FAIL"):
            print("rosetta3: prefix: could not download the base", flush=True)
            sys.exit(1)
        directory = args[2]
        os.makedirs(os.path.join(directory, "root", "Applications"), exist_ok=True)
        os.makedirs(os.path.join(directory, ".rosetta3"), exist_ok=True)
        lines = ["version: 1", "created_with:", '  runtime_version: "0.1.0"']
        if "--arch" in args:
            lines += ["base:", '  project: "minidarwin"', '  arch: "%s"' % args[args.index("--arch") + 1]]
        with open(os.path.join(directory, ".rosetta3", "state.yaml"), "w") as f:
            f.write("\\n".join(lines) + "\\n")
        with open(record + ".prefix", "w") as f:
            json.dump({{"argv": args, "setup": os.environ.get("ROSETTA3_PREFIX_SETUP")}}, f)
        print("created " + directory, flush=True)
        sys.exit(0)
    if args[:2] == ["prefix", "repair"]:
        print("nothing to repair", flush=True)
        sys.exit(0)
    if args[:1] == ["run"]:
        env = {{k: v for k, v in os.environ.items() if k.startswith("ROSETTA3_")}}
        with open(record, "w") as f:
            json.dump({{"argv": args, "env": env, "cwd": os.getcwd()}}, f)
        print("guest output", flush=True)
        code = int(os.environ.get("FAKE_ROSETTA3_EXIT", "-1"))
        if code >= 0:
            sys.exit(code)
        time.sleep(60)
        sys.exit(0)
    sys.exit(2)
    """
)


class Sandbox(unittest.TestCase):
    """Points every XDG directory and HOME at a scratch directory."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rostrap-test-"))
        self._saved_env = dict(os.environ)
        os.environ["HOME"] = str(self.root / "home")
        for variable, name in (
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_DATA_HOME", "data"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_STATE_HOME", "state"),
        ):
            os.environ[variable] = str(self.root / name)
        for name in ("ROSETTA3_PREFIX", "ROSETTA3_DEAD_FLAGS", "FAKE_ROSETTA3_EXIT", "FAKE_ROSETTA3_PREFIX_FAIL"):
            os.environ.pop(name, None)
        (self.root / "home").mkdir()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.root, ignore_errors=True)

    def fake_rosetta3(self) -> Path:
        path = self.root / "bin" / "rosetta3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE_ROSETTA3.format(python=sys.executable))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        os.environ["FAKE_ROSETTA3_RECORD"] = str(self.root / "record.json")
        return path

    def record(self, timeout: float = 10.0) -> dict:
        """What the fake rosetta3 recorded about its `run`. The process writes
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
