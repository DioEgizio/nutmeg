#!/usr/bin/env python3
"""Install what the Flatpak has beside the nutmeg package, under PREFIX/share:
the menu entries and nutmeg's icon, from `nutmeg.desktop.flatpak_files`, and
the AppStream metadata, with nutmeg's version as its release. The manifest's
build commands run it:

    python3 flatpak/install_data.py /app
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from nutmeg import __version__, desktop, paths  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} PREFIX", file=sys.stderr)
        return 2
    share = Path(sys.argv[1]) / "share"
    files = dict(desktop.flatpak_files())
    # SOURCE_DATE_EPOCH, when set, makes the date that of the commit.
    when = datetime.datetime.fromtimestamp(
        int(os.environ.get("SOURCE_DATE_EPOCH") or datetime.datetime.now().timestamp()),
        datetime.timezone.utc,
    )
    metainfo = (HERE / f"{paths.APP_ID}.metainfo.xml").read_text("utf-8")
    metainfo = metainfo.replace("@VERSION@", __version__).replace("@DATE@", when.strftime("%Y-%m-%d"))
    files[f"metainfo/{paths.APP_ID}.metainfo.xml"] = metainfo.encode()
    for name, data in sorted(files.items()):
        path = share / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
