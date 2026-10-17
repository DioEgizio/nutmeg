"""scripts/release.py, the steps the nightly and release workflows share, run
on scratch git repositories."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT

SCRIPT = ROOT / "scripts" / "release.py"
STAMP = "202610171200"


class ReleaseScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="nutmeg-release-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.git("init", "-q", "-b", "main")
        for key, value in (
            ("user.name", "Test"),
            ("user.email", "test@example.com"),
            ("commit.gpgSign", "false"),
            ("tag.gpgSign", "false"),
        ):
            self.git("config", key, value)
        (self.repo / "nutmeg").mkdir()

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout

    def commit(self, message: str, version: str | None = None) -> str:
        if version is not None:
            (self.repo / "nutmeg/__init__.py").write_text(f'"""nutmeg."""\n\n__version__ = "{version}"\n')
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], cwd=self.repo, capture_output=True, text=True
        )

    def output(self, *arguments: str) -> str:
        result = self.run_script(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def refused(self, pattern: str, *arguments: str) -> None:
        result = self.run_script(*arguments)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stderr, pattern)

    def test_nightly_versions(self):
        self.commit("Start", "0.2.0")
        self.assertEqual(self.output("nightly-version", "--stamp", STAMP), f"0.2.0.dev{STAMP}\n")
        # After 0.2.0 is released, nightly builds lead to the next version.
        self.git("tag", "v0.2.0")
        self.assertEqual(self.output("nightly-version", "--stamp", STAMP), f"0.2.1.dev{STAMP}\n")
        self.commit("Start 0.3.0", "0.3.0")
        self.assertEqual(self.output("nightly-version", "--stamp", STAMP), f"0.3.0.dev{STAMP}\n")
        # Versions compare as numbers: 0.10.0 is newer than 0.3.0.
        self.git("tag", "v0.10.0")
        self.assertEqual(self.output("nightly-version", "--stamp", STAMP), f"0.10.1.dev{STAMP}\n")
        self.refused("not a time stamp", "nightly-version", "--stamp", "today")

    def test_check(self):
        first = self.commit("Start", "0.1.0")
        self.assertEqual(self.output("check", "--sha", first), "version=0.1.0\ntag=v0.1.0\ntitle=nutmeg 0.1.0\n")
        self.git("tag", "v0.1.0", first)
        self.refused("v0.1.0 is already released", "check", "--sha", first)
        self.refused("not a release version", "check", "--sha", self.commit("Version bump", "0.2.0.dev1"))
        self.refused("not newer than the latest release, v0.1.0", "check", "--sha", self.commit("x", "0.0.9"))
        self.refused("0.1 is already released|not newer", "check", "--sha", self.commit("x", "0.1"))
        bump = self.commit("Version bump to 0.2.0", "0.2.0")
        self.assertIn("tag=v0.2.0\n", self.output("check", "--sha", bump))

    def release_files(self, version: str) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="nutmeg-dist-"))
        self.addCleanup(shutil.rmtree, folder, True)
        for name in (
            f"nutmeg-{version}-x86_64.flatpak",
            f"nutmeg-{version}.tar.gz",
            f"nutmeg-{version}-aarch64.flatpak",
            f"nutmeg-{version}-x86_64.AppImage",
            f"nutmeg-{version}-py3-none-any.whl",
            f"nutmeg-{version}-aarch64.AppImage",
            f"nutmeg-{version}.pyz",
            "SHA256SUMS",
        ):
            (folder / name).write_text(name)
        self.output("checksums", str(folder))
        return folder

    def test_checksums(self):
        folder = self.release_files("0.2.0")
        lines = (folder / "SHA256SUMS").read_text().splitlines()
        expected = sorted(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
            for path in folder.iterdir()
            if path.name != "SHA256SUMS"
        )
        self.assertEqual(sorted(lines), expected)
        self.assertEqual(len(lines), 7)

    def test_release_notes(self):
        self.commit("Start", "0.1.0")
        self.git("tag", "v0.1.0")
        thing = self.commit("Add a thing")
        bump = self.commit("Version bump to 0.2.0", "0.2.0")
        folder = self.release_files("0.2.0")
        text = self.output(
            "notes", "--kind", "release", "--sha", bump, "--tag", "v0.2.0", "--version", "0.2.0",
            "--dir", str(folder), "--repo", "frolleks/nutmeg",
        )
        self.assertTrue(text.startswith("## Changes since v0.1.0\n\n"), text)
        # Newest first, as git log lists them.
        self.assertIn(f"- {bump[:7]} Version bump to 0.2.0\n- {thing[:7]} Add a thing\n", text)
        self.assertNotIn("Start", text)
        self.assertIn("https://github.com/frolleks/nutmeg/compare/v0.1.0...v0.2.0", text)
        files = [line.split("`")[1] for line in text.split("## Files\n")[1].splitlines() if line.startswith("- ")]
        self.assertEqual(
            files,
            [
                "nutmeg-0.2.0.pyz",
                "nutmeg-0.2.0-py3-none-any.whl",
                "nutmeg-0.2.0.tar.gz",
                "nutmeg-0.2.0-aarch64.flatpak",
                "nutmeg-0.2.0-x86_64.flatpak",
                "nutmeg-0.2.0-aarch64.AppImage",
                "nutmeg-0.2.0-x86_64.AppImage",
                "SHA256SUMS",
            ],
        )
        self.assertIn("`flatpak install --user nutmeg-0.2.0-x86_64.flatpak`", text)
        self.assertIn("- `nutmeg-0.2.0-x86_64.AppImage`: the AppImage for x86-64 PCs: make it executable with `chmod +x nutmeg-0.2.0-x86_64.AppImage`, then run it.\n", text)

    def test_the_first_release_lists_every_commit(self):
        self.commit("Start", "0.1.0")
        bump = self.commit("Version bump")
        text = self.output(
            "notes", "--kind", "release", "--sha", bump, "--tag", "v0.1.0", "--version", "0.1.0",
            "--dir", str(self.release_files("0.1.0")),
        )
        self.assertIn("## Changes\n\n", text)
        self.assertIn(" Start\n", text)
        self.assertNotIn("compare", text)

    def test_nightly_notes(self):
        self.commit("Start", "0.1.0")
        self.git("tag", "nightly-20261016-2341")
        change = self.commit("Fix a thing")
        folder = self.release_files(f"0.1.0.dev{STAMP}")
        arguments = (
            "notes", "--kind", "nightly", "--tag", f"nightly-{STAMP[:8]}-{STAMP[8:]}",
            "--version", f"0.1.0.dev{STAMP}", "--dir", str(folder), "--built", "2026-10-17 12:00 UTC",
        )
        text = self.output(*arguments, "--sha", change)
        self.assertTrue(text.startswith(f"Automatic pre-release of {change[:7]} on `main`, built 2026-10-17 12:00 UTC.\n"))
        self.assertIn(f"## Changes since nightly-20261016-2341\n\n- {change[:7]} Fix a thing\n", text)
        self.git("tag", "nightly-20261017-1200")
        self.assertIn("No commits since nightly-20261017-1200", self.output(*arguments, "--sha", change))


if __name__ == "__main__":
    unittest.main()
