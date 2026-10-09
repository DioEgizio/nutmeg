"""Small file helpers shared by the modules that persist state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` so that a reader sees the old or the new file,
    never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, value: object) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    write_atomic(path, text.encode("utf-8"))
