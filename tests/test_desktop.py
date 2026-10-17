"""Desktop entries, checked against GLib's own reading of them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from importlib import resources
from pathlib import Path

from nutmeg import desktop, paths, roblox

from helpers import ROBLOX_ICONS, Sandbox, icns, png, roblox_zip

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
except (ImportError, ValueError):
    Gio = None


class ExecTests(Sandbox):
    def recorder(self) -> list[str]:
        """A command that records the arguments it is given, for `recorded`."""
        script = self.root / "dir with spaces" / "record argv.py"
        script.parent.mkdir(exist_ok=True)
        out = self.root / "argv.json"
        script.write_text(f"import json, sys\njson.dump(sys.argv[1:], open({str(out)!r}, 'w'))\n")
        return [sys.executable, str(script)]

    def recorded(self) -> list[str]:
        out = self.root / "argv.json"
        deadline = time.monotonic() + 10
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        return json.loads(out.read_text())

    @unittest.skipIf(Gio is None, "needs PyGObject")
    def test_glib_runs_exactly_the_arguments_given(self):
        tricky = ["plain", "two words", "$HOME", "back\\slash", "100%", '"quoted"', "it's", "~tilde", "a;b|c&d", "`tick`", "é"]
        entry = self.root / "test.desktop"
        entry.write_text(
            "[Desktop Entry]\nType=Application\nName=Test\n"
            f"Exec={desktop.exec_value(self.recorder() + tricky, '%u')}\n"
        )
        info = Gio.DesktopAppInfo.new_from_filename(str(entry))
        self.assertIsNotNone(info)
        url = "roblox-player:1+launchmode:play+gameinfo:abc"
        self.assertTrue(info.launch_uris([url], None))
        self.assertEqual(self.recorded(), tricky + [url])

    @unittest.skipIf(Gio is None, "needs PyGObject")
    def test_the_settings_action(self):
        entry = self.root / desktop.PLAYER_ID
        entry.write_text(desktop.player_entry(self.recorder()))
        info = Gio.DesktopAppInfo.new_from_filename(str(entry))
        self.assertEqual(info.list_actions(), ["open-settings"])
        self.assertEqual(info.get_action_name("open-settings"), "nutmeg Settings")
        info.launch_action("open-settings", None)
        self.assertEqual(self.recorded(), ["settings"])

    def test_control_characters_are_refused(self):
        with self.assertRaises(Exception):
            desktop.exec_value(["a\nb"])

    def test_command_for_a_source_checkout(self):
        command = desktop.nutmeg_command()
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(command, ["env", f"PYTHONPATH={root}", sys.executable, "-m", "nutmeg"])


class EntryTests(Sandbox):
    @unittest.skipIf(shutil.which("desktop-file-validate") is None, "needs desktop-file-utils")
    def test_entries_validate(self):
        command = desktop.nutmeg_command()
        for name, text in (("player", desktop.player_entry(command)), ("settings", desktop.settings_entry(command))):
            path = self.root / f"io.github.frolleks.Nutmeg.{name}.desktop"
            path.write_text(text)
            result = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "", result.stdout)

    @unittest.skipIf(shutil.which("xdg-mime") is None, "needs xdg-utils")
    def test_install_registers_both_schemes(self):
        os.environ.pop("XDG_CURRENT_DESKTOP", None)
        os.environ.pop("DESKTOP_SESSION", None)
        done = desktop.install()
        self.assertTrue(desktop.installed())
        self.assertIn(f"x-scheme-handler/roblox-player opens with {desktop.PLAYER_ID}", done)
        for mime in desktop.MIME_TYPES:
            self.assertEqual(desktop.default_handler(mime), desktop.PLAYER_ID)
        text = (paths.applications_dir() / desktop.PLAYER_ID).read_text()
        self.assertIn("MimeType=x-scheme-handler/roblox-player;x-scheme-handler/roblox;\n", text)
        self.assertIn(" launch %u\n", text)
        removed = desktop.uninstall()
        self.assertEqual(len(removed), 3)
        self.assertFalse(desktop.installed())


def entry_icon(name: str) -> str:
    for line in (paths.applications_dir() / name).read_text().splitlines():
        if line.startswith("Icon="):
            return line[len("Icon="):]
    raise AssertionError(f"{name} has no Icon line")


def theme_files() -> list[str]:
    root = paths.icons_dir()
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


ROBLOX_PNGS = [
    "128x128/apps/io.github.frolleks.Nutmeg.png",
    "256x256/apps/io.github.frolleks.Nutmeg.png",
    "32x32/apps/io.github.frolleks.Nutmeg.png",
]
SETTINGS_SVG = "scalable/apps/io.github.frolleks.Nutmeg.Settings.svg"


class IcnsTests(unittest.TestCase):
    def test_only_square_pngs_are_taken(self):
        data = icns(
            [
                (b"icnV", b"\0\0\0\0"),
                (b"ic07", png(128)),
                (b"ic04", b"ARGB" + b"\0" * 32),
                (b"ic09", b"\0\0\0\x0cjP  \r\n\x87\n"),
                (b"ic08", png(256, 128)),
                (b"ic13", png(256)),
            ]
        )
        self.assertEqual(roblox.icns_pngs(data), {128: png(128), 256: png(256)})

    def test_damaged_files(self):
        good = icns([(b"ic07", png(128))])
        self.assertEqual(roblox.icns_pngs(b"not an icns file"), {})
        self.assertEqual(roblox.icns_pngs(good[:4]), {})
        # An entry running past the end, or claiming less than its own header.
        self.assertEqual(roblox.icns_pngs(good[:-1]), {})
        self.assertEqual(roblox.icns_pngs(good[:12] + b"\0\0\0\x04" + good[16:]), {})
        self.assertEqual(roblox.icns_pngs(good + b"ic13\xff\xff\xff\xff"), {128: png(128)})


@unittest.skipIf(shutil.which("xdg-mime") is None, "needs xdg-utils")
class IconTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("XDG_CURRENT_DESKTOP", None)
        os.environ.pop("DESKTOP_SESSION", None)

    def install_roblox(self, **zip_options) -> roblox.Build:
        build = roblox.Build("version-0123456789abcdef", "0.741.0.1", "x86_64")
        archive = self.root / "archive.zip"
        archive.write_bytes(roblox_zip(**zip_options))
        roblox.install(archive, build, lambda *_: None, threading.Event())
        roblox.save_installed(build)
        return build

    def test_before_roblox_is_installed(self):
        done = desktop.install()
        self.assertEqual(entry_icon(desktop.PLAYER_ID), "applications-games")
        self.assertEqual(entry_icon(desktop.SETTINGS_ID), "io.github.frolleks.Nutmeg.Settings")
        self.assertIn("Roblox's icon replaces applications-games once a launch installs Roblox", done)
        self.assertEqual(theme_files(), [SETTINGS_SVG])
        packaged = (resources.files("nutmeg") / "data" / "nutmeg.svg").read_bytes()
        self.assertEqual((paths.icons_dir() / SETTINGS_SVG).read_bytes(), packaged)

        # The first launch installs a build, and the entry gets its icon.
        before = (paths.applications_dir() / desktop.PLAYER_ID).read_text()
        build = self.install_roblox()
        self.assertTrue(desktop.use_roblox_icon(build.bundle()))
        after = (paths.applications_dir() / desktop.PLAYER_ID).read_text()
        self.assertEqual(after, before.replace("Icon=applications-games\n", "Icon=io.github.frolleks.Nutmeg\n"))
        self.assertEqual(theme_files(), ROBLOX_PNGS + [SETTINGS_SVG])
        for size, data in ROBLOX_ICONS.items():
            self.assertEqual((paths.icons_dir() / f"{size}x{size}/apps/io.github.frolleks.Nutmeg.png").read_bytes(), data)
        # Later launches of the same build change nothing.
        self.assertFalse(desktop.use_roblox_icon(build.bundle()))

    def test_after_roblox_is_installed(self):
        self.install_roblox()
        done = desktop.install()
        self.assertEqual(entry_icon(desktop.PLAYER_ID), "io.github.frolleks.Nutmeg")
        self.assertIn(f"wrote Roblox's icon to {paths.icons_dir()} (32x32, 128x128, 256x256)", done)
        self.assertEqual(theme_files(), ROBLOX_PNGS + [SETTINGS_SVG])
        desktop.uninstall()
        self.assertEqual(theme_files(), [])

    def test_a_new_build_with_fewer_sizes(self):
        build = self.install_roblox()
        desktop.install()
        icon = roblox.icon_file(build.bundle())
        icon.write_bytes(icns([(b"ic08", png(256, rgba=b"\0\0\0\xff"))]))
        self.assertTrue(desktop.use_roblox_icon(build.bundle()))
        self.assertEqual(theme_files(), ["256x256/apps/io.github.frolleks.Nutmeg.png", SETTINGS_SVG])

    def test_a_build_without_an_icon(self):
        self.install_roblox(icon=False)
        done = desktop.install()
        self.assertEqual(entry_icon(desktop.PLAYER_ID), "applications-games")
        self.assertTrue(any(line.startswith("could not copy Roblox's icon: ") for line in done), done)

    def test_nothing_is_written_without_the_entries(self):
        build = self.install_roblox()
        self.assertFalse(desktop.use_roblox_icon(build.bundle()))
        self.assertFalse(paths.icons_dir().exists())

    @unittest.skipIf(Gio is None, "needs PyGObject")
    def test_glib_reads_the_icon_names(self):
        self.install_roblox()
        desktop.install()
        for name, icon in ((desktop.PLAYER_ID, desktop.PLAYER_ICON), (desktop.SETTINGS_ID, desktop.SETTINGS_ICON)):
            info = Gio.DesktopAppInfo.new_from_filename(str(paths.applications_dir() / name))
            self.assertEqual(info.get_icon().get_names()[0], icon)


if __name__ == "__main__":
    unittest.main()
