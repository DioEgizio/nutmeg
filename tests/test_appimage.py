"""nutmeg in its AppImage: the AppRun that sets the bundled libraries up, the
environment the programs nutmeg starts get instead, and updating the
AppImage in place. scripts/build_appimage.py builds the AppImage itself, and
runs these tests on what it bundles."""

from __future__ import annotations

import glob
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nutmeg import desktop, paths, rosemary, updater
from nutmeg.config import Config

from helpers import ROOT, Sandbox, Server

sys.path.insert(0, str(ROOT / "scripts"))
import build_appimage  # noqa: E402

MACHINE = platform.machine()
# A Python standing in for the bundled one, which prints what it was run with.
FAKE_PYTHON = "import json, os, sys\njson.dump({'argv': sys.argv[1:], 'env': dict(os.environ)}, sys.stdout)\n"


def fake_appimage(version: str) -> bytes:
    return f'#!/bin/sh\n[ "$1" = --version ] && echo "nutmeg {version}"\n'.encode()


class AppRunTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.appdir = Path(folder.name).resolve()
        python = self.appdir / "usr/bin/python3"
        python.parent.mkdir(parents=True)
        python.write_text(f"#!{sys.executable}\n{FAKE_PYTHON}")
        python.chmod(0o755)
        build_appimage.write_apprun(self.appdir / "AppRun", '$(dirname "$(readlink -f "$0")")', '-m nutmeg "$@"')

    def run_apprun(self, env: dict[str, str]) -> dict:
        result = subprocess.run(
            [str(self.appdir / "AppRun"), "launch", "a b"], env=env, capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)

    def host(self, **variables: str) -> dict[str, str]:
        return {"PATH": os.environ["PATH"], "HOME": "/home/me", "LANG": "C.UTF-8", **variables}

    def assert_restored(self, env: dict[str, str], host: dict[str, str]) -> None:
        with mock.patch.dict(os.environ, env, clear=True):
            restored = paths.host_env()
        names = [*host, *build_appimage.APPRUN_VARS]
        self.assertEqual({n: restored.get(n) for n in names}, {n: host.get(n) for n in names})
        self.assertEqual([n for n in restored if n.startswith("NUTMEG_")], [])

    def test_runs_the_bundled_python_with_its_libraries(self):
        host = self.host(GI_TYPELIB_PATH="/opt/typelibs", SSL_CERT_FILE="/etc/my.pem")
        ran = self.run_apprun(host)
        self.assertEqual(ran["argv"], ["-I", "-m", "nutmeg", "launch", "a b"])
        env = ran["env"]
        self.assertEqual(env["GI_TYPELIB_PATH"], f"{self.appdir}/usr/lib/girepository-1.0")
        self.assertEqual(env["NUTMEG_HOST_GI_TYPELIB_PATH"], "/opt/typelibs")
        self.assertEqual(env["GIO_LAUNCH_DESKTOP"], f"{self.appdir}/usr/bin/gio-launch-desktop")
        self.assertNotIn("NUTMEG_HOST_GIO_LAUNCH_DESKTOP", env)
        # Certificates the user chose are kept.
        self.assertEqual(env["SSL_CERT_FILE"], "/etc/my.pem")
        self.assertEqual(env[paths.APPIMAGE_VARS].split(), list(build_appimage.APPRUN_VARS))
        self.assert_restored(env, host)

    def test_a_variable_set_empty_stays_set(self):
        host = self.host(GTK_EXE_PREFIX="")
        env = self.run_apprun(host)["env"]
        self.assertEqual(env["NUTMEG_HOST_GTK_EXE_PREFIX"], "")
        self.assert_restored(env, host)

    def test_started_by_a_nutmeg_appimage(self):
        # Given what an AppImage passed on, the system's values are put back
        # before they are kept.
        host = self.host(GI_TYPELIB_PATH="/opt/typelibs")
        first = self.run_apprun(host)["env"]
        second = self.run_apprun(first)["env"]
        self.assertEqual(second["NUTMEG_HOST_GI_TYPELIB_PATH"], "/opt/typelibs")
        self.assertNotIn("NUTMEG_HOST_GDK_PIXBUF_MODULE_FILE", second)
        self.assert_restored(second, host)

    def test_only_variable_names_are_read(self):
        marker = self.appdir / "marker"
        host = self.host(**{paths.APPIMAGE_VARS: f"GI_TYPELIB_PATH $(touch${{IFS}}{marker}) lower"})
        self.run_apprun(host)
        self.assertFalse(marker.exists())


class TypelibTests(unittest.TestCase):
    def test_header(self):
        found = glob.glob("/usr/lib/*/girepository-1.0/Gtk-4.0.typelib") + glob.glob("/usr/lib/girepository-1.0/Gtk-4.0.typelib")
        if not found:
            self.skipTest("needs GTK 4's typelib")
        namespace, dependencies, libraries = build_appimage.typelib_header(Path(found[0]))
        self.assertEqual(namespace, "Gtk-4.0")
        self.assertIn("Gdk-4.0", dependencies)
        self.assertEqual(libraries, ["libgtk-4.so.1"])


class EnvironmentTests(Sandbox):
    def run_from_appimage(self, image: str = "/apps/nutmeg.AppImage") -> None:
        os.environ.update(
            {
                "APPIMAGE": image,
                "APPDIR": str(Path(paths.__file__).resolve().parent.parent),
                "ARGV0": image,
                paths.APPIMAGE_VARS: "GI_TYPELIB_PATH GIO_MODULE_DIR",
                "NUTMEG_HOST_GI_TYPELIB_PATH": "/opt/typelibs",
                "GI_TYPELIB_PATH": "/tmp/.mount_nutmeg/usr/lib/girepository-1.0",
                "GIO_MODULE_DIR": "/tmp/.mount_nutmeg/usr/lib/gio/modules",
            }
        )

    def test_not_an_appimage(self):
        self.assertIsNone(paths.appimage())
        self.assertEqual(paths.host_env(), dict(os.environ))

    def test_started_from_another_appimage(self):
        os.environ.update(APPIMAGE="/apps/Other.AppImage", APPDIR="/tmp/.mount_Other")
        self.assertIsNone(paths.appimage())

    def test_menu_entries_run_the_appimage(self):
        self.run_from_appimage()
        self.assertEqual(paths.appimage(), Path("/apps/nutmeg.AppImage"))
        self.assertEqual(desktop.nutmeg_command(), ["/apps/nutmeg.AppImage"])
        self.assertIn("\nExec=/apps/nutmeg.AppImage launch %u\n", desktop.player_entry(desktop.nutmeg_command()))

    def test_roblox_gets_the_systems_environment(self):
        self.run_from_appimage()
        env = rosemary.run_env(Config())
        self.assertEqual(env["GI_TYPELIB_PATH"], "/opt/typelibs")
        for name in ("GIO_MODULE_DIR", "APPIMAGE", "APPDIR", "ARGV0", paths.APPIMAGE_VARS, "NUTMEG_HOST_GI_TYPELIB_PATH"):
            self.assertNotIn(name, env)
        self.assertEqual(env["ROSEMARY_PREFIX"], str(paths.default_prefix()))

    def test_restarts_the_appimage(self):
        self.run_from_appimage()
        with mock.patch.object(updater.os, "execvpe", side_effect=OSError("stopped here")) as execvpe:
            with self.assertRaises(OSError):
                updater.restart(["launch", "roblox-player:1"], "0.1.0")
        file, argv, env = execvpe.call_args.args
        self.assertEqual((file, argv), ("/apps/nutmeg.AppImage", ["/apps/nutmeg.AppImage", "launch", "roblox-player:1"]))
        self.assertEqual(env[updater.UPDATED_FROM], "0.1.0")
        self.assertEqual(env["GI_TYPELIB_PATH"], "/opt/typelibs")
        self.assertNotIn("APPIMAGE", env)


class UpdateTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.server = Server()
        self.addCleanup(self.server.close)
        for patch in (
            mock.patch.object(updater, "RELEASES_URL", f"{self.server.url}/releases"),
            mock.patch.object(updater, "LATEST_URL", f"{self.server.url}/releases/latest"),
            mock.patch.object(updater, "DOWNLOADS", f"{self.server.url}/download/"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.folder = self.root / "Applications"
        self.folder.mkdir()
        self.image = self.folder / f"nutmeg-0.1.0-{MACHINE}.AppImage"
        self.image.write_bytes(fake_appimage("0.1.0"))
        self.image.chmod(0o755)
        os.environ.update(APPIMAGE=str(self.image), APPDIR=str(Path(paths.__file__).resolve().parent.parent))

    def version_of(self, path: Path) -> str:
        return subprocess.run([str(path), "--version"], capture_output=True, text=True).stdout.strip()

    def test_replaces_the_appimage_in_place(self):
        other = "aarch64" if MACHINE == "x86_64" else "x86_64"
        files = {
            f"nutmeg-0.2.0-{MACHINE}.AppImage": fake_appimage("0.2.0"),
            f"nutmeg-0.3.0-{other}.AppImage": b"for another machine",
            "nutmeg-0.3.0.pyz": b"for the .pyz",
        }
        self.server.publish_nutmeg("v0.2.0", files, prerelease=False)
        installation = updater.installation()
        self.assertEqual((installation.kind, installation.path), ("appimage", self.image))
        release = updater.newer(installation, "0.1.0")
        self.assertEqual(release.name, f"nutmeg-0.2.0-{MACHINE}.AppImage")
        updater.install(installation, release)
        self.assertEqual(self.version_of(self.image), "nutmeg 0.2.0")
        self.assertEqual(self.image.stat().st_mode & 0o777, 0o755)
        self.assertEqual([p.name for p in self.folder.iterdir()], [self.image.name])

    def test_an_appimage_that_is_another_version(self):
        self.server.publish_nutmeg("v0.2.0", {f"nutmeg-0.2.0-{MACHINE}.AppImage": fake_appimage("0.2.9")}, prerelease=False)
        installation = updater.installation()
        with self.assertRaisesRegex(updater.NutmegError, "printed 'nutmeg 0.2.9'"):
            updater.install(installation, updater.newer(installation, "0.1.0"))
        self.assertEqual(self.version_of(self.image), "nutmeg 0.1.0")
        self.assertEqual([p.name for p in self.folder.iterdir()], [self.image.name])

    @unittest.skipIf(os.geteuid() == 0, "root can change any file")
    def test_an_appimage_nutmeg_cannot_change(self):
        self.folder.chmod(0o555)
        self.addCleanup(self.folder.chmod, 0o755)
        installation = updater.installation()
        self.assertIsNone(installation.kind)
        self.assertIn("cannot change", installation.description)


if __name__ == "__main__":
    unittest.main()
