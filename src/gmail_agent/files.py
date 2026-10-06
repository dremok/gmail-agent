"""Turning attachment names from strangers into safe local file names."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

MAX_NAME_BYTES = 200
_FORBIDDEN = set('<>:"|?*')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{p}{i}" for p in ("COM", "LPT") for i in range(10)
}


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """Strip directories, control characters and leading dots, so the name stays in its folder."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in _FORBIDDEN)
    name = name.strip().lstrip(".").rstrip(". ")
    if not name:
        name = fallback
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return _truncate(name)


def _truncate(name: str) -> str:
    if len(name.encode()) <= MAX_NAME_BYTES:
        return name
    stem, suffix = os.path.splitext(name)
    if len(suffix.encode()) > 20:
        stem, suffix = name, ""
    budget = MAX_NAME_BYTES - len(suffix.encode())
    stem = stem.encode()[:budget].decode(errors="ignore")
    return stem + suffix


def write_new_file(out_dir: Path, filename: str, data: bytes) -> Path:
    """Write `data` under `out_dir` without ever replacing an existing file.

    `report.pdf` becomes `report_1.pdf`, `report_2.pdf`, ... if taken. Uses O_EXCL, so two
    downloads racing for the same name cannot clobber each other.
    """
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    stem, suffix = os.path.splitext(name)
    n = 0
    while True:
        candidate = out_dir / (name if n == 0 else f"{stem}_{n}{suffix}")
        if candidate.parent != out_dir:  # cannot happen after safe_filename; belt and braces
            raise ValueError(f"refusing to write outside {out_dir}: {candidate}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return candidate
