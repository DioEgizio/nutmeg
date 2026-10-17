"""nutmeg in its Flatpak sandbox, with `paths.in_flatpak` patched to say so:
the host folders it uses, the files the Flatpak ships, and how a nutmeg in
one sandbox reaches the Roblox started in another."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from importlib import resources
from pathlib import Path
from unittest import mock

from nutmeg import butter, desktop, paths, presence, processes, roblox, updater

from helpers import ROBLOX_ICONS, Sandbox, roblox_zip


class FlatpakSandbox(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(paths, "in_flatpak", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ["FLATPAK_ID"] = paths.APP_ID
        self.host_share = self.root / "home/.local/share"


class PathTests(FlatpakSandbox):
    def test_host_folders(self):
        self.assertEqual(paths.host_data_home(), self.host_share)
        self.assertEqual(paths.icons_dir(), self.host_share / "icons/hicolor")
        self.assertEqual(paths.runtime_dir(), self.root / "run/app" / paths.APP_ID)
        # nutmeg's own files stay in the sandbox's folders.
        self.assertEqual(paths.data_dir(), self.root / "data/nutmeg")

    def test_butter_runners_on_the_host(self):
        directory = self.host_share / "butter/runners/r"
        (directory / "bin").mkdir(parents=True)
        (directory / "runner.toml").write_text('name = "R"\nkind = "binary"\ntag = "v1.0.0"\nexecutable = "bin/rosemary"\n')
        executable = directory / "bin/rosemary"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        self.assertEqual([(r.executable, r.flatpak) for r in butter.runners()], [(executable, False)])

    def test_nutmeg_runs_itself_by_the_flatpaks_command(self):
        self.assertEqual(desktop.nutmeg_command(), ["/app/bin/nutmeg"])

    def test_no_updates(self):
        found = updater.installation()
        self.assertIsNone(found.kind)
        self.assertIn("Flatpak", found.description)


class FilesTests(unittest.TestCase):
    def test_what_the_flatpak_ships(self):
        files = desktop.flatpak_files()
        player = files[f"applications/{desktop.PLAYER_ID}"].decode()
        settings = files[f"applications/{desktop.SETTINGS_ID}"].decode()
        # Flatpak's export turns `nutmeg` into `flatpak run … --command=nutmeg`.
        self.assertIn("\nExec=nutmeg launch %u\n", player)
        self.assertIn("\nMimeType=x-scheme-handler/roblox-player;x-scheme-handler/roblox;\n", player)
        self.assertIn(f"\nIcon={desktop.PLAYER_ICON}\n", player)
        self.assertIn("\n[Desktop Action open-settings]\nName=nutmeg Settings\nExec=nutmeg settings\n", player)
        self.assertIn("\nExec=nutmeg settings\n", settings)
        packaged = (resources.files("nutmeg") / "data" / "nutmeg.svg").read_bytes()
        self.assertEqual(files[f"icons/hicolor/scalable/apps/{desktop.SETTINGS_ICON}.svg"], packaged)

    @unittest.skipIf(shutil.which("desktop-file-validate") is None, "needs desktop-file-utils")
    def test_the_entries_are_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, data in desktop.flatpak_files().items():
                if name.endswith(".desktop"):
                    path = Path(folder) / Path(name).name
                    path.write_bytes(data)
                    result = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True)
                    self.assertEqual(result.stdout + result.stderr, "", name)


class DesktopTests(FlatpakSandbox):
    def install_roblox(self) -> roblox.Build:
        build = roblox.Build("version-0123456789abcdef", "0.741.0.1", "x86_64")
        archive = self.root / "archive.zip"
        archive.write_bytes(roblox_zip())
        roblox.install(archive, build, lambda *_: None, threading.Event())
        roblox.save_installed(build)
        return build

    def player_icons(self) -> list[Path]:
        return sorted(paths.icons_dir().glob(f"*/apps/{desktop.PLAYER_ICON}.png"))

    def test_the_first_launch_adds_roblox_icon_to_the_host_theme(self):
        self.assertTrue(desktop.installed())
        done = desktop.install()
        self.assertIn("Roblox's icon is added once a launch installs Roblox", done)
        self.assertIn(desktop.FLATPAK_DEFAULT_COMMAND, done[-1])
        # The entries are the Flatpak's: nothing is written for them.
        self.assertFalse(paths.applications_dir().exists())
        self.assertEqual(self.player_icons(), [])

        build = self.install_roblox()
        self.assertTrue(desktop.use_roblox_icon(build.bundle()))
        self.assertFalse(desktop.use_roblox_icon(build.bundle()))
        self.assertEqual(len(self.player_icons()), len(ROBLOX_ICONS))
        self.assertTrue(all(icon.is_relative_to(self.host_share / "icons/hicolor") for icon in self.player_icons()))
        self.assertFalse(paths.applications_dir().exists())

        self.assertEqual(desktop.uninstall(), [f"removed Roblox's icon from {paths.icons_dir()}"])
        self.assertEqual(self.player_icons(), [])
        self.assertEqual(desktop.uninstall(), [])


class ControlTests(FlatpakSandbox):
    """The watcher's socket, asked by a nutmeg that cannot see its Roblox."""

    def serve(self, running: dict[int, str]) -> threading.Event:
        stop = threading.Event()
        log = presence.Log(None)
        control = presence.Control(log, stop)
        for patch in (
            mock.patch.object(processes, "running", return_value=running),
            mock.patch.object(processes, "terminate", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        control.start()
        self.addCleanup(control.close)
        return stop

    def test_running_and_stop(self):
        stop = self.serve({41: "version-a-x86_64", 42: "version-a-x86_64"})
        self.assertTrue(processes.control_socket().is_socket())
        self.assertEqual(processes.running_elsewhere(), {"version-a-x86_64"})
        self.assertFalse(stop.is_set())
        self.assertTrue(processes.stop_elsewhere())
        processes.terminate.assert_called_once_with([41, 42])
        # The watcher stops, so that the next Roblox's watcher can start.
        self.assertTrue(stop.is_set())

    def test_roblox_that_will_not_close(self):
        stop = self.serve({41: "version-a-x86_64"})
        processes.terminate.return_value = [41]
        self.assertFalse(processes.stop_elsewhere())
        self.assertFalse(stop.is_set())

    def test_a_stale_socket_is_replaced_and_removed(self):
        processes.control_socket().parent.mkdir(parents=True)
        processes.control_socket().write_text("left over")
        stop = threading.Event()
        control = presence.Control(presence.Log(None), stop)
        with mock.patch.object(processes, "running", return_value={}):
            control.start()
            self.assertEqual(processes.running_elsewhere(), set())
            control.close()
        self.assertFalse(processes.control_socket().exists())

    def test_nobody_to_ask(self):
        self.assertEqual(processes.running_elsewhere(), set())
        self.assertTrue(processes.stop_elsewhere())

    def test_never_asked_outside_the_flatpak(self):
        self.serve({41: "version-a-x86_64"})
        with mock.patch.object(paths, "in_flatpak", return_value=False):
            self.assertEqual(processes.running_elsewhere(), set())


if __name__ == "__main__":
    unittest.main()
