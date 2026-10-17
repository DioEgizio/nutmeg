"""Tests of nutmeg updating itself: reading GitHub's release list, checking
and installing a .pyz or a wheel, finding other nutmeg processes, and
starting again. A local server stands in for GitHub."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nutmeg import processes, updater
from nutmeg.errors import NutmegError

from helpers import ROOT, Sandbox, Server, fake_wheel, nutmeg_pyz


class VersionTests(unittest.TestCase):
    def test_kinds(self):
        self.assertTrue(updater.is_release("0.1.0"))
        self.assertFalse(updater.is_release("0.1.0.dev202610170131"))
        self.assertFalse(updater.is_release("latest"))

    def test_order(self):
        ordered = ["0.1.0.dev9", "0.1.0.dev202610170131", "0.1", "0.1.1.dev1", "0.1.1", "0.2.0", "1.0.0.dev1", "10.0"]
        self.assertEqual(sorted(reversed(ordered), key=updater.version_key), ordered)
        self.assertEqual(updater.version_key("0.1"), updater.version_key("0.1.0"))
        for other in ("", "latest", "0.1.0+local", "0.1.0rc1", "v0.1.0", "0.1.0.dev"):
            self.assertIsNone(updater.version_key(other), other)


class ServerTests(Sandbox):
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


class NewestTests(ServerTests):
    def test_the_newest_file_for_each_kind(self):
        self.server.publish_nutmeg("n1", {"nutmeg-0.1.0.dev1.pyz": b"a", "nutmeg-0.1.0.dev1-py3-none-any.whl": b"b"})
        self.server.publish_nutmeg("n3", {"nutmeg-0.1.0.dev3.pyz": b"c", "nutmeg-latest.pyz": b"d"})
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.0.dev2-py3-none-any.whl": b"e"})
        releases = json.loads(self.server.routes["/releases"])
        # Ignored: a draft, a release without SHA256SUMS, files elsewhere,
        # and a release among the nightly builds.
        download = f"{self.server.url}/download"
        releases.append({"draft": True, "prerelease": True, "assets": [{"name": "nutmeg-9.0.pyz", "browser_download_url": f"{download}/d/x"}]})
        releases.append({"prerelease": True, "assets": [{"name": "nutmeg-8.0.pyz", "browser_download_url": f"{download}/e/x"}]})
        releases.append(
            {
                "prerelease": True,
                "assets": [
                    {"name": "nutmeg-7.0.pyz", "browser_download_url": "https://example.com/nutmeg-7.0.pyz"},
                    {"name": "SHA256SUMS", "browser_download_url": f"{download}/f/SHA256SUMS"},
                ],
            }
        )
        releases.append(
            {
                "prerelease": False,
                "assets": [
                    {"name": "nutmeg-6.0.pyz", "browser_download_url": f"{download}/g/nutmeg-6.0.pyz"},
                    {"name": "SHA256SUMS", "browser_download_url": f"{download}/g/SHA256SUMS"},
                ],
            }
        )
        releases.append("not a release")
        self.server.routes["/releases"] = json.dumps(releases).encode()
        pyz = updater.newest("pyz", nightly=True)
        self.assertEqual((pyz.version, pyz.name), ("0.1.0.dev3", "nutmeg-0.1.0.dev3.pyz"))
        self.assertEqual(pyz.url, f"{self.server.url}/download/n3/nutmeg-0.1.0.dev3.pyz")
        self.assertEqual(pyz.checksums, f"{self.server.url}/download/n3/SHA256SUMS")
        self.assertEqual(updater.newest("wheel", nightly=True).version, "0.1.0.dev2")
        installation = updater.Installation("pyz", Path("x.pyz"), "")
        self.assertEqual(updater.newer(installation, "0.1.0.dev2").version, "0.1.0.dev3")
        self.assertIsNone(updater.newer(installation, "0.1.0.dev3"))
        self.assertIsNone(updater.newer(updater.Installation(None, Path("."), ""), "0.0.1.dev1"))

    def test_each_copy_follows_its_own_kind(self):
        self.server.publish_nutmeg("n1", {"nutmeg-0.1.0.dev5.pyz": b"a"})
        self.server.publish_nutmeg("v0.1.0", {"nutmeg-0.1.0.pyz": b"b"}, prerelease=False)
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.1.dev1.pyz": b"c"})
        installation = updater.Installation("pyz", Path("x.pyz"), "")
        # A nightly build goes on to the next nightly build, past the release.
        self.assertEqual(updater.newer(installation, "0.1.0.dev5").version, "0.1.1.dev1")
        # A release goes on to the next release, and never to a nightly
        # build. It asks for GitHub's latest release only.
        self.server.requests.clear()
        self.assertEqual(updater.newer(installation, "0.0.9").version, "0.1.0")
        self.assertIsNone(updater.newer(installation, "0.1.0"))
        self.assertEqual(self.server.requests, ["/releases/latest"] * 2)
        # GitHub's latest release, should it be a pre-release, is not one.
        latest = json.loads(self.server.routes["/releases/latest"])
        self.server.routes["/releases/latest"] = json.dumps(dict(latest, prerelease=True)).encode()
        self.assertIsNone(updater.newer(installation, "0.0.9"))

    def test_no_release_yet(self):
        self.server.publish_nutmeg("n1", {"nutmeg-0.1.0.dev5.pyz": b"a"})
        self.assertNotIn("/releases/latest", self.server.routes)
        self.assertIsNone(updater.newest("pyz", nightly=False))
        self.assertIsNone(updater.newer(updater.Installation("pyz", Path("x.pyz"), ""), "0.0.9"))

    def test_failures(self):
        self.server.routes["/releases"] = 503
        with self.assertRaisesRegex(NutmegError, "HTTP 503"):
            updater.newest("pyz", nightly=True)
        self.server.routes["/releases"] = b"<html>"
        with self.assertRaisesRegex(NutmegError, "Could not read"):
            updater.newest("pyz", nightly=True)
        self.server.routes["/releases"] = b"{}"
        self.assertIsNone(updater.newest("pyz", nightly=True))
        self.server.routes["/releases/latest"] = 503
        with self.assertRaisesRegex(NutmegError, "HTTP 503"):
            updater.newest("pyz", nightly=False)
        self.server.routes["/releases/latest"] = b"[]"
        self.assertIsNone(updater.newest("pyz", nightly=False))


class PyzTests(ServerTests):
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.root / "apps"
        self.folder.mkdir()
        self.archive = self.folder / "nutmeg-0.1.0.dev1.pyz"
        self.archive.write_bytes(nutmeg_pyz("0.1.0.dev1"))
        self.archive.chmod(0o750)
        self.installation = updater.Installation("pyz", self.archive, "")

    def version_of(self, path: Path) -> str:
        return subprocess.run([sys.executable, str(path), "--version"], capture_output=True, text=True).stdout.strip()

    def test_replaces_the_file_in_place(self):
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.0.dev2.pyz": nutmeg_pyz("0.1.0.dev2")})
        release = updater.newer(self.installation, "0.1.0.dev1")
        updater.install(self.installation, release)
        self.assertEqual(self.version_of(self.archive), "nutmeg 0.1.0.dev2")
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o750)
        self.assertEqual([p.name for p in self.folder.iterdir()], [self.archive.name])

    def check_refused(self, pattern: str) -> None:
        before = self.archive.read_bytes()
        release = updater.newer(self.installation, "0.1.0.dev1")
        with self.assertRaisesRegex(NutmegError, pattern):
            updater.install(self.installation, release)
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual([p.name for p in self.folder.iterdir()], [self.archive.name])

    def test_a_file_that_does_not_match_its_checksum(self):
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.0.dev2.pyz": nutmeg_pyz("0.1.0.dev2")}, sums="0" * 64 + "  nutmeg-0.1.0.dev2.pyz\n")
        self.check_refused("does not match its checksum")

    def test_a_file_missing_from_the_checksums(self):
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.0.dev2.pyz": nutmeg_pyz("0.1.0.dev2")}, sums="")
        self.check_refused("does not list nutmeg-0.1.0.dev2.pyz")

    def test_a_file_that_is_another_version(self):
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.0.dev2.pyz": nutmeg_pyz("0.1.0.dev9")})
        self.check_refused("printed 'nutmeg 0.1.0.dev9'")

    def test_a_file_that_is_not_nutmeg(self):
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.0.dev2.pyz": b"PK\x05\x06" + b"\0" * 18})
        self.check_refused("nutmeg 0.1.0.dev2 printed ''")


class WheelTests(ServerTests):
    def test_installs_with_the_environments_pip(self):
        environment = self.root / "env"
        made = subprocess.run([sys.executable, "-m", "venv", str(environment)], capture_output=True)
        python = environment / "bin" / "python"
        if made.returncode != 0 or subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True).returncode:
            self.skipTest("needs venv with pip")
        old = self.root / "nutmeg-0.1.0.dev1-py3-none-any.whl"
        old.write_bytes(fake_wheel("0.1.0.dev1"))
        subprocess.run([str(python), "-m", "pip", "install", "-q", "--no-index", str(old)], check=True, cwd="/")
        self.server.publish_nutmeg("n2", {"nutmeg-0.1.0.dev2-py3-none-any.whl": fake_wheel("0.1.0.dev2")})
        installation = updater.Installation("wheel", environment, "", python=str(python))
        updater.install(installation, updater.newer(installation, "0.1.0.dev1"))
        result = subprocess.run([str(python), "-m", "nutmeg", "--version"], capture_output=True, text=True, cwd="/")
        self.assertEqual(result.stdout.strip(), "nutmeg 0.1.0.dev2")


class InstallationTests(unittest.TestCase):
    def test_a_checkout(self):
        found = updater.installation()
        self.assertIsNone(found.kind)
        self.assertEqual(found.path, ROOT)
        self.assertIn("source checkout", found.description)

    def test_a_pyz(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        archive = Path(folder.name) / "nutmeg.pyz"
        archive.write_bytes(nutmeg_pyz("0.1.0.dev1"))
        code = "from nutmeg import updater; i = updater.installation(); print(i.kind, i.path)"
        env = dict(os.environ, PYTHONPATH=str(archive))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd="/")
        self.assertEqual(result.stdout.strip(), f"pyz {archive.resolve()}")


class ProcessTests(Sandbox):
    def test_other_nutmeg_processes(self):
        script = self.root / "bin" / "nutmeg"
        script.parent.mkdir()
        script.write_text("import time\ntime.sleep(30)\n")
        nutmeg = subprocess.Popen([sys.executable, str(script)])
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "nutmeg"])
        try:
            deadline = time.monotonic() + 10
            while nutmeg.pid not in processes.nutmeg_processes() and time.monotonic() < deadline:
                time.sleep(0.05)
            found = processes.nutmeg_processes()
            self.assertIn(nutmeg.pid, found)
            self.assertNotIn(other.pid, found)
            self.assertNotIn(os.getpid(), found)
        finally:
            for process in (nutmeg, other):
                process.kill()
                process.wait()

    def test_the_command_lines_that_run_nutmeg(self):
        for arguments, runs in (
            (["/usr/bin/python3", "-m", "nutmeg", "watch"], True),
            (["/home/me/.local/share/pipx/venvs/nutmeg/bin/python", "/home/me/.local/bin/nutmeg", "launch"], True),
            (["python3", "Downloads/nutmeg-0.1.0.dev202610170131.pyz"], True),
            (["/tmp/.mount_nutmegAbC123/usr/bin/python3", "-I", "-m", "nutmeg", "launch"], True),
            (["/home/me/Applications/nutmeg-0.2.0-x86_64.AppImage", "launch", "roblox-player:1"], False),
            (["python3", "-m", "nutmegs"], False),
            (["vim", "notes/nutmeg.txt"], False),
            (["nutmeg"], False),
        ):
            self.assertEqual(processes._runs_nutmeg(arguments), runs, arguments)


class RestartTests(Sandbox):
    def test_execs_nutmeg_again(self):
        with mock.patch.object(updater.os, "execvpe", side_effect=OSError("stopped here")) as execvpe:
            with self.assertRaises(OSError):
                updater.restart(["launch", "roblox-player:1"], "0.1.0.dev1")
        file, argv, env = execvpe.call_args.args
        self.assertEqual(argv, ["env", f"PYTHONPATH={ROOT}", sys.executable, "-m", "nutmeg", "launch", "roblox-player:1"])
        self.assertEqual(file, "env")
        self.assertEqual(env[updater.UPDATED_FROM], "0.1.0.dev1")

    def test_the_new_process_is_nutmeg(self):
        code = "from nutmeg import updater; updater.restart(['--version'], '0.0.1')"
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd="/")
        self.assertEqual(result.stdout.strip(), "nutmeg 0.1.0")


if __name__ == "__main__":
    unittest.main()
