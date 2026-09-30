"""Desktop entries, checked against GLib's own reading of them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from rostrap import desktop, paths

from helpers import Sandbox

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
except (ImportError, ValueError):
    Gio = None


class ExecTests(Sandbox):
    @unittest.skipIf(Gio is None, "needs PyGObject")
    def test_glib_runs_exactly_the_arguments_given(self):
        script = self.root / "dir with spaces" / "record argv.py"
        script.parent.mkdir()
        out = self.root / "argv.json"
        script.write_text(f"import json, sys\njson.dump(sys.argv[1:], open({str(out)!r}, 'w'))\n")
        tricky = ["plain", "two words", "$HOME", "back\\slash", "100%", '"quoted"', "it's", "~tilde", "a;b|c&d", "`tick`", "é"]
        entry = self.root / "test.desktop"
        entry.write_text(
            "[Desktop Entry]\nType=Application\nName=Test\n"
            f"Exec={desktop.exec_value([sys.executable, str(script)] + tricky, '%u')}\n"
        )
        info = Gio.DesktopAppInfo.new_from_filename(str(entry))
        self.assertIsNotNone(info)
        url = "roblox-player:1+launchmode:play+gameinfo:abc"
        self.assertTrue(info.launch_uris([url], None))
        deadline = time.monotonic() + 10
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertEqual(json.loads(out.read_text()), tricky + [url])

    def test_control_characters_are_refused(self):
        with self.assertRaises(Exception):
            desktop.exec_value(["a\nb"])

    def test_command_for_a_source_checkout(self):
        command = desktop.rostrap_command()
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(command, ["env", f"PYTHONPATH={root}", sys.executable, "-m", "rostrap"])


class EntryTests(Sandbox):
    @unittest.skipIf(shutil.which("desktop-file-validate") is None, "needs desktop-file-utils")
    def test_entries_validate(self):
        command = desktop.rostrap_command()
        for name, text in (("player", desktop.player_entry(command)), ("settings", desktop.settings_entry(command))):
            path = self.root / f"io.github.frolleks.Rostrap.{name}.desktop"
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
        self.assertEqual(len(removed), 2)
        self.assertFalse(desktop.installed())


if __name__ == "__main__":
    unittest.main()
