#!/usr/bin/env python3
"""Build nutmeg's release files from the committed tree.

    python3 scripts/build_nightly.py --version 0.1.0.dev202609302341 --out dist

Writes to --out a wheel, a source archive, a zipapp (`.pyz`, which runs with
the system's Python and its PyGObject) and SHA256SUMS. The version is stamped
into a copy of HEAD taken with `git archive`, so the checkout is not changed.
Each file is then checked: the wheel is installed into a scratch virtual
environment and run, and the zipapp is run.

Needs the `build` package (pip install build) and git.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "nutmeg"

_VERSION_LINE = re.compile(r'^__version__ = "[^"\n]*"$', re.MULTILINE)
# A release or development version, without a local part: GitHub renames
# release files that contain "+", which would break the wheel's file name.
_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*(\.dev[0-9]+)?$")

_ZIPAPP_MAIN = "import sys\n\nfrom nutmeg.cli import main\n\nsys.exit(main())\n"


def export_head(destination: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(destination, filter="data")
        else:
            tar.extractall(destination)


def stamp_version(tree: Path, version: str) -> None:
    init = tree / PACKAGE / "__init__.py"
    text = init.read_text("utf-8")
    stamped, count = _VERSION_LINE.subn(f'__version__ = "{version}"', text)
    if count != 1:
        raise SystemExit(f"{init}: expected one __version__ line, found {count}")
    init.write_text(stamped, "utf-8")


def build_zipapp(tree: Path, out: Path, version: str, scratch: Path) -> Path:
    staging = scratch / "zipapp"
    shutil.copytree(tree / PACKAGE, staging / PACKAGE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (staging / "__main__.py").write_text(_ZIPAPP_MAIN, "utf-8")
    target = out / f"{PACKAGE}-{version}.pyz"
    zipapp.create_archive(staging, target, interpreter="/usr/bin/env python3", compressed=True)
    return target


def check_version_output(command: list[str], version: str) -> None:
    result = subprocess.run(command + ["--version"], capture_output=True, text=True)
    expected = f"{PACKAGE} {version}"
    if result.returncode != 0 or result.stdout.strip() != expected:
        raise SystemExit(
            f"{' '.join(command)} --version printed {result.stdout.strip()!r} "
            f"(exit status {result.returncode}, stderr {result.stderr.strip()!r}); "
            f"expected {expected!r}"
        )


def check_wheel(wheel: Path, tree: Path, version: str, scratch: Path) -> None:
    # Every file of the package, so that data such as the icon is checked too.
    sources = {
        path.relative_to(tree).as_posix()
        for path in (tree / PACKAGE).rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(wheel) as archive:
        missing = sources - set(archive.namelist())
    if missing:
        raise SystemExit(f"{wheel.name} is missing {', '.join(sorted(missing))}")
    environment = scratch / "wheel-env"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    subprocess.run(
        [str(environment / "bin" / "pip"), "install", "--quiet", "--no-index", str(wheel)],
        check=True,
    )
    check_version_output([str(environment / "bin" / PACKAGE)], version)


def write_checksums(out: Path) -> None:
    lines = []
    for path in sorted(out.iterdir()):
        if path.name != "SHA256SUMS" and path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}\n")
    (out / "SHA256SUMS").write_text("".join(lines), "utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True, help="the version to stamp, such as 0.1.0.dev202609302341")
    parser.add_argument("--out", required=True, type=Path, help="an empty or missing directory for the files")
    args = parser.parse_args()
    if not _VERSION.match(args.version):
        parser.error(f"{args.version!r} is not a version like 0.1.0 or 0.1.0.dev202609302341")
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nutmeg-build-") as scratch_name:
        scratch = Path(scratch_name)
        tree = scratch / "tree"
        tree.mkdir()
        export_head(tree)
        stamp_version(tree, args.version)
        subprocess.run([sys.executable, "-m", "build", "--outdir", str(out), str(tree)], check=True)
        wheels = sorted(out.glob("*.whl"))
        sdists = sorted(out.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise SystemExit(f"expected one wheel and one source archive in {out}, found {sorted(p.name for p in out.iterdir())}")
        check_wheel(wheels[0], tree, args.version, scratch)
        pyz = build_zipapp(tree, out, args.version, scratch)
        check_version_output([sys.executable, str(pyz)], args.version)
    write_checksums(out)
    for path in sorted(out.iterdir()):
        print(f"{path.name}  {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
