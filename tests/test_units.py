"""Tests of the pieces that do not need the network, a display or rosetta3."""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from nutmeg import config as config_module
from nutmeg import fastflags, paths, processes, protocol, roblox, rosetta
from nutmeg.config import Config, ConfigError
from nutmeg.errors import NutmegError

from helpers import Sandbox, fat, macho, roblox_zip


class ProtocolTests(unittest.TestCase):
    def test_accepts_both_schemes(self):
        for url in (
            "roblox-player:1+launchmode:play+gameinfo:abc+launchtime:1+placelauncherurl:https%3A%2F%2Fx",
            "roblox://experiences/start?placeId=1818",
            "ROBLOX-PLAYER:1+launchmode:app",
        ):
            self.assertEqual(protocol.check(url), url)

    def test_redacts_the_ticket(self):
        url = "roblox-player:1+launchmode:play+gameinfo:SECRET_TICKET+launchtime:1+browsertrackerid:2"
        self.assertEqual(
            protocol.redact(f"run x -protocolString {url}"),
            "run x -protocolString roblox-player:1+launchmode:play+gameinfo:<hidden>+launchtime:1+browsertrackerid:2",
        )
        self.assertEqual(protocol.redact("roblox://experiences/start?placeId=1"), "roblox://experiences/start?placeId=1")

    def test_refuses_other_links(self):
        for url in ("https://roblox.com", "roblox-player:", "file:///etc/passwd", "", "roblox", "roblox-player:1\n2"):
            with self.assertRaises(protocol.InvalidURL):
                protocol.check(url)


class FastFlagTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(
            [fastflags.kind_of(v) for v in (True, 144, "x", 1.5, None, [1], {"a": 1})],
            ["bool", "int", "string", "json", "json", "json", "json"],
        )
        self.assertEqual(
            [fastflags.kind_for_name(n) for n in ("FFlagX", "DFFlagX", "SFFlagX", "FIntX", "DFIntX", "FStringX", "DFStringX")],
            ["bool", "bool", "bool", "int", "int", "string", "string"],
        )
        for name in ("FFlag", "FLogX", "Other", ""):
            self.assertIsNone(fastflags.kind_for_name(name))

    def test_values(self):
        self.assertEqual(fastflags.value_of("int", " -42 ", False), -42)
        self.assertIs(fastflags.value_of("bool", "ignored", True), True)
        self.assertEqual(fastflags.value_of("string", " spaced ", False), " spaced ")
        self.assertEqual(fastflags.value_of("json", '{"a": [1, null]}', False), {"a": [1, None]})
        for kind, text in (("int", "1.5"), ("int", "1_000"), ("int", ""), ("json", "{"), ("json", "")):
            with self.subTest(kind=kind, text=text), self.assertRaises(ValueError):
                fastflags.value_of(kind, text, False)
        for value in (True, 7, "s", 2.5, None, [1, "x"], {"k": False}):
            kind = fastflags.kind_of(value)
            self.assertEqual(fastflags.value_of(kind, fastflags.text_of(value), bool(value)), value)

    def test_collect(self):
        rows = [("  FFlagA ", "bool", "", True), ("FIntB", "int", "60", False)]
        self.assertEqual(fastflags.collect(rows), {"FFlagA": True, "FIntB": 60})
        with self.assertRaisesRegex(ConfigError, "row 2 has no name"):
            fastflags.collect([("A", "bool", "", True), (" ", "bool", "", True)])
        with self.assertRaisesRegex(ConfigError, "A is listed twice"):
            fastflags.collect([("A", "bool", "", True), ("A ", "int", "1", False)])
        with self.assertRaisesRegex(ConfigError, "FIntB needs a whole number"):
            fastflags.collect([("FIntB", "int", "sixty", False)])

    def test_parse_object(self):
        self.assertEqual(fastflags.parse_object("  "), {})
        self.assertEqual(fastflags.parse_object('{"A": 1, "B": true}'), {"A": 1, "B": True})
        for text in ("[1]", '"x"', "{"):
            with self.assertRaises(ConfigError):
                fastflags.parse_object(text)


class ConfigTests(Sandbox):
    def test_defaults_when_nothing_is_saved(self):
        self.assertEqual(config_module.load(), Config())
        self.assertEqual(Config().prefix(), paths.default_prefix())

    def test_round_trip(self):
        config = Config(
            rosetta3_path="/opt/rosetta3/target/release/rosetta3",
            prefix_path="~/prefixes/roblox",
            architecture="x86_64",
            swap_interval="0",
            menu_ctrl=False,
            extra_env={"RUST_LOG": "debug", "A": "b=c"},
            fast_flags={"FFlagExample": True},
        )
        config_module.save(config)
        self.assertEqual(config_module.load(), config)
        self.assertEqual(config.prefix(), Path.home() / "prefixes/roblox")

    def test_rejects_bad_values(self):
        bad = [
            {"architecture": "ppc"},
            {"engine": "fast"},
            {"swap_interval": "2"},
            {"menu_ctrl": "yes"},
            {"prefix_path": "relative/prefix"},
            {"extra_env": {"ROSETTA3_PREFIX": "/x"}},
            {"extra_env": {"1BAD": "x"}},
            {"extra_env": {"GOOD": 1}},
            {"fast_flags": [1, 2]},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                Config.from_dict(data)

    def test_unreadable_file_is_an_error_not_the_defaults(self):
        paths.config_file().parent.mkdir(parents=True)
        paths.config_file().write_text("{not json")
        with self.assertRaises(ConfigError):
            config_module.load()

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(Config.from_dict({"future_setting": 1}), Config())


class MachOTests(Sandbox):
    def check(self, data: bytes) -> set:
        path = self.root / "binary"
        path.write_bytes(data)
        return roblox.macho_architectures(path)

    def test_thin_and_fat(self):
        self.assertEqual(self.check(macho("x86_64")), {"x86_64"})
        self.assertEqual(self.check(macho("arm64")), {"arm64"})
        self.assertEqual(self.check(fat("x86_64", "arm64")), {"x86_64", "arm64"})

    def test_not_macho(self):
        self.assertEqual(self.check(b"\x7fELF" + b"\0" * 60), set())
        # A Java class file begins with the fat magic, then its version.
        self.assertEqual(self.check(bytes.fromhex("cafebabe00000034") + b"\0" * 64), set())
        self.assertEqual(self.check(b"\xcf"), set())


class InstallTests(Sandbox):
    build = roblox.Build(guid="version-0123456789abcdef", version="0.1.0.1", arch="x86_64")

    def install(self, data: bytes, build: roblox.Build | None = None) -> Path:
        archive = self.root / "archive.zip"
        archive.write_bytes(data)
        return roblox.install(archive, build or self.build, lambda *_: None, threading.Event())

    def test_installs_as_roblox_app_and_keeps_only_the_executable_bit(self):
        os.umask(0o022)
        link = ("RobloxPlayer.app/Contents/Frameworks/Current", b"Versions/A", 0o120777)
        program = self.install(roblox_zip(extra=[link]))
        bundle = self.build.bundle()
        self.assertEqual(program, bundle / "Contents/MacOS/RobloxPlayer")
        self.assertEqual(program.stat().st_mode & 0o777, 0o755)
        resource = bundle / "Contents/Resources/content.txt"
        self.assertEqual(resource.stat().st_mode & 0o777, 0o644)
        self.assertEqual(os.readlink(bundle / "Contents/Frameworks/Current"), "Versions/A")
        self.assertEqual(roblox.installed_executable(self.build), program)
        self.assertEqual(roblox.installed_keys(), [self.build.key])
        self.assertEqual([p.name for p in paths.versions_dir().iterdir()], [self.build.key])

    def test_refuses_unsafe_archives(self):
        cases = [
            [("RobloxPlayer.app/../../escape", b"x", 0o100644)],
            [("/tmp/absolute", b"x", 0o100644)],
            [("Other.app/file", b"x", 0o100644)],
            [("RobloxPlayer.app/link", b"../../outside", 0o120777)],
            [("RobloxPlayer.app/link", b"/etc", 0o120777)],
            [("RobloxPlayer.app/fifo", b"", 0o010644)],
        ]
        for extra in cases:
            with self.subTest(extra=extra[0][0]):
                with self.assertRaises(NutmegError):
                    self.install(roblox_zip(extra=extra))
                self.assertEqual(list(paths.versions_dir().iterdir()), [])
        self.assertFalse((self.root / "escape").exists())

    def test_wrong_architecture_is_refused(self):
        with self.assertRaisesRegex(NutmegError, "expected to be arm64"):
            self.install(roblox_zip("x86_64"), roblox.Build(self.build.guid, "", "arm64"))
        self.assertEqual(list(paths.versions_dir().iterdir()), [])

    def test_corruption_is_detected(self):
        name = "RobloxPlayer.app/Contents/Resources/content.txt"
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                source = zipfile.ZipFile(io.BytesIO(roblox_zip()))
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as rewritten:
                    for info in source.infolist():
                        contents = source.read(info)
                        info.compress_type = compression
                        rewritten.writestr(info, contents)
                data = bytearray(buffer.getvalue())
                info = zipfile.ZipFile(io.BytesIO(bytes(data))).getinfo(name)
                start = info.header_offset
                name_length, extra_length = struct.unpack("<HH", data[start + 26 : start + 30])
                body = start + 30 + name_length + extra_length
                data[body + info.compress_size // 2] ^= 0xFF
                with self.assertRaises(NutmegError):
                    self.install(bytes(data))
                self.assertEqual(list(paths.versions_dir().iterdir()), [])

    def test_cancel_leaves_nothing(self):
        cancel = threading.Event()
        cancel.set()
        archive = self.root / "archive.zip"
        archive.write_bytes(roblox_zip())
        with self.assertRaises(Exception) as caught:
            roblox.install(archive, self.build, lambda *_: None, cancel)
        self.assertEqual(type(caught.exception).__name__, "Cancelled")
        self.assertEqual(list(paths.versions_dir().iterdir()), [])

    def test_fast_flags(self):
        self.install(roblox_zip())
        bundle = self.build.bundle()
        roblox.write_fast_flags(bundle, {"FIntTargetRefreshRate": 144})
        written = bundle / "Contents/MacOS/ClientSettings/ClientAppSettings.json"
        self.assertEqual(json.loads(written.read_text()), {"FIntTargetRefreshRate": 144})
        roblox.write_fast_flags(bundle, {})
        self.assertFalse(written.exists())

    def test_installed_record(self):
        self.assertIsNone(roblox.load_installed())
        roblox.save_installed(self.build)
        self.assertEqual(roblox.load_installed(), self.build)
        paths.installed_file().write_text('{"guid": "version-../../x", "arch": "x86_64"}')
        self.assertIsNone(roblox.load_installed())

    def test_remove_versions(self):
        for key in ("version-aa-x86_64", "version-bb-x86_64", "version-cc-x86_64", ".tmp-version-dd-x86_64-1"):
            (paths.versions_dir() / key).mkdir(parents=True)
        removed = roblox.remove_versions({"version-aa-x86_64"}, {"version-bb-x86_64"})
        self.assertEqual(removed, ["version-cc-x86_64"])
        roblox.remove_leftovers()
        self.assertEqual(sorted(p.name for p in paths.versions_dir().iterdir()), ["version-aa-x86_64", "version-bb-x86_64"])

    def test_zip_urls(self):
        x86 = roblox.Build("version-1", "", "x86_64").zip_urls()
        arm = roblox.Build("version-1", "", "arm64").zip_urls()
        self.assertEqual(x86[0], "https://setup.rbxcdn.com/mac/version-1-RobloxPlayer.zip")
        self.assertEqual(arm[0], "https://setup.rbxcdn.com/mac/arm64/version-1-RobloxPlayer.zip")
        self.assertEqual(len(x86), len(roblox.CDN_HOSTS))


class RosettaTests(Sandbox):
    def test_base_arch_from_state_file(self):
        prefix = self.root / "prefix"
        (prefix / ".rosetta3").mkdir(parents=True)
        state = prefix / ".rosetta3/state.yaml"
        self.assertFalse(rosetta.prefix_created(prefix))
        state.write_text('version: 1\ncreated_with:\n  runtime_version: "0.1.0"\n')
        self.assertTrue(rosetta.prefix_created(prefix))
        self.assertIsNone(rosetta.prefix_base_arch(prefix))
        # The shape rosetta3's State::render writes.
        state.write_text(
            '# Written by `rosetta3 prefix`. See docs/prefixes.md.\nversion: 1\ncreated_with:\n'
            '  runtime_version: "0.1.0"\nbase:\n  project: "minidarwin"\n  arch: "aarch64"\n'
            '  sequence: 9\n  tag: "v9.0.0"\naccepted_sequence: 9\n'
        )
        self.assertEqual(rosetta.prefix_base_arch(prefix), "aarch64")
        state.write_text("base:\n  arch: x86_64\n")
        self.assertEqual(rosetta.prefix_base_arch(prefix), "x86_64")

    def test_environment(self):
        inherited = {"ROSETTA3_DEAD_FLAGS": "1", "ROSETTA3_PREFIX": "/elsewhere", "PATH": "/bin", "ROSETTA3_OBJC": "builtin"}
        env = rosetta.run_env(Config(), inherited)
        self.assertNotIn("ROSETTA3_DEAD_FLAGS", env)
        self.assertEqual(env["ROSETTA3_PREFIX"], str(paths.default_prefix()))
        self.assertEqual(env["ROSETTA3_OBJC"], "builtin")
        self.assertEqual(env["PATH"], "/bin")
        config = Config(
            swap_interval="-1",
            decorations="server",
            menu="strip",
            menu_ctrl=False,
            confine_pointer=True,
            dead_flags=True,
            call_return=True,
            extra_env={"RUST_BACKTRACE": "1"},
        )
        env = rosetta.run_env(config, {})
        self.assertEqual(
            {k: v for k, v in env.items() if k != "ROSETTA3_PREFIX"},
            {
                "ROSETTA3_SWAP_INTERVAL": "-1",
                "ROSETTA3_DECORATIONS": "server",
                "ROSETTA3_MENU": "strip",
                "ROSETTA3_MENU_CTRL": "0",
                "ROSETTA3_CONFINE_POINTER": "1",
                "ROSETTA3_DEAD_FLAGS": "1",
                "ROSETTA3_CALL_RETURN": "1",
                "RUST_BACKTRACE": "1",
            },
        )

    def test_command_line(self):
        program = Path("/data/versions/v/Roblox.app/Contents/MacOS/RobloxPlayer")
        tool = Path("/bin/rosetta3")
        self.assertEqual(rosetta.run_argv(tool, Config(), program, None), ["/bin/rosetta3", "run", str(program)])
        self.assertEqual(
            rosetta.run_argv(tool, Config(engine="emulated"), program, "roblox-player:1"),
            ["/bin/rosetta3", "run", "--emulated", str(program), "-protocolString", "roblox-player:1"],
        )

    def test_build_choice(self):
        with mock.patch.object(rosetta, "host_arch", return_value="x86_64"):
            self.assertEqual(rosetta.build_arch(Config()), "x86_64")
            with self.assertRaises(NutmegError):
                rosetta.build_arch(Config(architecture="arm64"))
        with mock.patch.object(rosetta, "host_arch", return_value="arm64"):
            self.assertEqual(rosetta.build_arch(Config()), "arm64")
            self.assertEqual(rosetta.build_arch(Config(architecture="x86_64")), "x86_64")
            self.assertEqual(rosetta.build_arch(Config(architecture="x86_64", engine="emulated")), "x86_64")
            for bad in (Config(architecture="x86_64", engine="native"), Config(engine="emulated")):
                with self.assertRaises(NutmegError):
                    rosetta.build_arch(bad)
        with mock.patch.object(rosetta, "host_arch", return_value=None):
            with self.assertRaises(NutmegError):
                rosetta.build_arch(Config())

    def test_find_rosetta3(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "nowhere")}):
            with self.assertRaises(NutmegError):
                rosetta.find_rosetta3(Config())
            tool = self.fake_rosetta3()
            self.assertEqual(rosetta.find_rosetta3(Config(rosetta3_path=str(tool))), tool)
        with mock.patch.dict(os.environ, {"PATH": str(tool.parent)}):
            self.assertEqual(rosetta.find_rosetta3(Config()), tool)
        with self.assertRaises(NutmegError):
            rosetta.find_rosetta3(Config(rosetta3_path=str(self.root)))

    def test_create_prefix(self):
        tool = self.fake_rosetta3()
        prefix = self.root / "prefix"
        lines = []
        rosetta.create_prefix(tool, prefix, "x86_64", lines.append, threading.Event())
        called = json.loads(Path(os.environ["FAKE_ROSETTA3_RECORD"] + ".prefix").read_text())
        self.assertEqual(called["argv"], ["prefix", "create", str(prefix), "--arch", "x86_64"])
        self.assertEqual(called["setup"], "download")
        self.assertEqual(lines, [f"created {prefix}"])
        self.assertEqual(rosetta.prefix_base_arch(prefix), "x86_64")

        other = self.root / "other"
        rosetta.create_prefix(tool, other, None, lines.append, threading.Event())
        called = json.loads(Path(os.environ["FAKE_ROSETTA3_RECORD"] + ".prefix").read_text())
        self.assertEqual(called["argv"], ["prefix", "create", str(other), "--no-base"])
        self.assertIsNone(rosetta.prefix_base_arch(other))

        os.environ["FAKE_ROSETTA3_PREFIX_FAIL"] = "1"
        with self.assertRaisesRegex(NutmegError, "could not download the base"):
            rosetta.create_prefix(tool, self.root / "third", "x86_64", lines.append, threading.Event())


class ProcessTests(Sandbox):
    def test_finds_and_stops_processes_running_a_build(self):
        program = paths.versions_dir() / "version-aa-x86_64/Roblox.app/Contents/MacOS/RobloxPlayer"
        guest = processes.GUEST_HOST_MOUNT + str(paths.versions_dir() / "version-bb-x86_64/Roblox.app/x")
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        client = subprocess.Popen(sleeper + [str(program)])
        child = subprocess.Popen(sleeper + [guest])
        unrelated = subprocess.Popen(sleeper + [str(self.root / "elsewhere")])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                found = processes.running()
                if len(found) == 2:
                    break
                time.sleep(0.05)
            self.assertEqual(found, {client.pid: "version-aa-x86_64", child.pid: "version-bb-x86_64"})
            self.assertEqual(processes.terminate(sorted(found)), [])
            self.assertEqual(processes.running(), {})
            self.assertIsNone(unrelated.poll())
        finally:
            for process in (client, child, unrelated):
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
