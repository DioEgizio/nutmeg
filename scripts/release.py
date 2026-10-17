#!/usr/bin/env python3
"""The steps of publishing nutmeg that the nightly and release workflows
share, kept out of the workflows' shell so that they can be run and tested
locally. Each works on the git checkout in the current folder.

    release.py nightly-version --stamp 202610172341
        The version of a nightly build: the version in nutmeg/__init__.py
        with .dev<stamp> after it. When that version, or a newer one, is
        already released, the version after the newest release is used
        instead (0.2.0 released gives 0.2.1.dev<stamp>), so that nightly
        builds made after a release are newer than the ones before it.

    release.py check --sha SHA
        Checks that commit SHA can be released: its nutmeg/__init__.py has a
        release version (such as 1.2.3) that has no tag yet and is newer than
        every release. Prints version=, tag= and title= lines for
        $GITHUB_OUTPUT.

    release.py checksums DIR
        Writes DIR/SHA256SUMS for the other files in DIR.

    release.py notes --kind nightly|release --sha SHA --tag TAG --version V
                     --dir DIR [--branch B] [--built WHEN] [--repo OWNER/NAME]
        Prints the release notes: what the build is, the commits since the
        previous release of the same kind, and the files in DIR.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

_RELEASE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_VERSION_LINE = re.compile(r'^__version__ = "([^"\n]*)"$', re.MULTILINE)
_ARCHITECTURES = {"x86_64": "x86-64 PCs", "aarch64": "ARM64 (AArch64) computers"}


class Refused(Exception):
    """A reason the step cannot go on, for the workflow's log."""


def _git(*arguments: str) -> str:
    return subprocess.run(["git", *arguments], check=True, capture_output=True, text=True).stdout


def _key(version: str) -> tuple[int, ...]:
    """Orders release versions; 0.1 and 0.1.0 are the same."""
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _version_in(text: str, where: str) -> str:
    match = _VERSION_LINE.search(text)
    if match is None:
        raise Refused(f"{where} has no __version__ line")
    return match.group(1)


def _releases() -> list[str]:
    """The versions of the release tags (vX.Y.Z), oldest first."""
    found = [tag[1:] for tag in _git("tag", "--list", "v*").split()]
    return sorted((v for v in found if _RELEASE.match(v)), key=_key)


def nightly_version(stamp: str) -> str:
    if not stamp.isdigit():
        raise Refused(f"{stamp!r} is not a time stamp of digits")
    base = _version_in(Path("nutmeg/__init__.py").read_text("utf-8"), "nutmeg/__init__.py")
    if not _RELEASE.match(base):
        raise Refused(f"nutmeg/__init__.py says {base}, which is not a version like 1.2.3")
    released = _releases()
    if released and _key(released[-1]) >= _key(base):
        parts = released[-1].split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        base = ".".join(parts)
    return f"{base}.dev{stamp}"


def check(sha: str) -> dict[str, str]:
    try:
        text = _git("show", f"{sha}:nutmeg/__init__.py")
    except subprocess.CalledProcessError as e:
        raise Refused(f"could not read nutmeg/__init__.py at {sha}: {e.stderr.strip()}") from e
    version = _version_in(text, f"nutmeg/__init__.py at {sha[:7]}")
    if not _RELEASE.match(version):
        raise Refused(
            f"nutmeg/__init__.py at {sha[:7]} says {version}, which is not a release version like 1.2.3"
        )
    tag = f"v{version}"
    released = _releases()
    if version in released or _git("tag", "--list", tag).strip():
        raise Refused(
            f"{tag} is already released. Set the new version in nutmeg/__init__.py in the version bump commit."
        )
    if released and _key(version) <= _key(released[-1]):
        raise Refused(f"{version} is not newer than the latest release, v{released[-1]}")
    return {"version": version, "tag": tag, "title": f"nutmeg {version}"}


def write_checksums(directory: Path) -> None:
    lines = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    (directory / "SHA256SUMS").write_text("".join(lines), "utf-8")


def _file_note(name: str, version: str) -> tuple[int, str]:
    """Where a release file is listed, and what is said about it."""
    if name == f"nutmeg-{version}.pyz":
        return 0, (
            f"run with `python3 {name}`. PyGObject and GTK 4 come from your distribution, "
            "as the README describes."
        )
    if name == f"nutmeg-{version}-py3-none-any.whl":
        return 1, f"install with `pipx install --system-site-packages ./{name}`."
    if name == f"nutmeg-{version}.tar.gz":
        return 2, "the source."
    flatpak = re.fullmatch(rf"nutmeg-{re.escape(version)}-(\w+)\.flatpak", name)
    if flatpak:
        machines = _ARCHITECTURES.get(flatpak.group(1), flatpak.group(1))
        return 3, f"the Flatpak for {machines}: install with `flatpak install --user {name}`."
    appimage = re.fullmatch(rf"nutmeg-{re.escape(version)}-(\w+)\.AppImage", name)
    if appimage:
        machines = _ARCHITECTURES.get(appimage.group(1), appimage.group(1))
        return 4, f"the AppImage for {machines}: make it executable with `chmod +x {name}`, then run it."
    if name == "SHA256SUMS":
        return 6, "checksums of the files above."
    return 5, ""


def notes(args: argparse.Namespace) -> str:
    pattern = "nightly-*" if args.kind == "nightly" else "v*"
    try:
        previous = _git("describe", "--tags", "--match", pattern, "--abbrev=0", args.sha).strip()
    except subprocess.CalledProcessError:
        previous = ""
    log = ["log", "--no-merges", "--format=- %h %s"]
    out = []
    if args.kind == "nightly":
        out += [f"Automatic pre-release of {args.sha[:7]} on `{args.branch}`, built {args.built}.", ""]
    if previous and not _git("rev-list", f"{previous}..{args.sha}").strip():
        out += [f"No commits since {previous}; this build was started by hand with `force`.", ""]
    elif previous:
        out += [f"## Changes since {previous}", "", _git(*log, f"{previous}..{args.sha}").rstrip(), ""]
        if args.kind == "release" and args.repo:
            out += [f"**Full changelog**: https://github.com/{args.repo}/compare/{previous}...{args.tag}", ""]
    else:
        commits = _git(*log, args.sha) if args.kind == "release" else _git(*log, "-n", "50", args.sha)
        out += ["## Changes", "", commits.rstrip(), ""]
    out += ["## Files", ""]
    files = sorted(
        (path.name for path in Path(args.dir).iterdir() if path.is_file()),
        key=lambda name: (_file_note(name, args.version)[0], name),
    )
    for name in files:
        note = _file_note(name, args.version)[1]
        out.append(f"- `{name}`: {note}" if note else f"- `{name}`")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("nightly-version")
    command.add_argument("--stamp", required=True)
    command = commands.add_parser("check")
    command.add_argument("--sha", required=True)
    command = commands.add_parser("checksums")
    command.add_argument("dir", type=Path)
    command = commands.add_parser("notes")
    command.add_argument("--kind", required=True, choices=["nightly", "release"])
    command.add_argument("--sha", required=True)
    command.add_argument("--tag", required=True)
    command.add_argument("--version", required=True)
    command.add_argument("--dir", required=True, type=Path)
    command.add_argument("--branch", default="main")
    command.add_argument("--built", default="")
    command.add_argument("--repo", default="")
    args = parser.parse_args()
    try:
        if args.command == "nightly-version":
            print(nightly_version(args.stamp))
        elif args.command == "check":
            for key, value in check(args.sha).items():
                print(f"{key}={value}")
        elif args.command == "checksums":
            write_checksums(args.dir)
        else:
            sys.stdout.write(notes(args))
    except Refused as e:
        print(f"release.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
