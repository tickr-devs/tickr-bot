"""Archived JSONL files: `<name>.jsonl` compressed in place to `<name>.jsonl.gz` by
`aperture maintain` (see `aperture.maintain`).

Every reader of capture and decision files opens them through `open_text`, so a reader given
the plain name still reads a day that was archived. The plain file wins when both exist: the
`.gz` is only renamed into place after it was verified to hold the same bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
from pathlib import Path
from typing import IO, TextIO

GZ_SUFFIX = ".gz"
TMP_SUFFIX = ".tmp"
CHUNK_BYTES = 1 << 20  # copy buffer: a unit size, not a tunable


def existing(path: Path) -> Path | None:
    """`path` if it is a file, else its archived `.gz` twin if that is a file, else None."""
    if path.is_file():
        return path
    archived = path.with_name(path.name + GZ_SUFFIX)
    return archived if archived.is_file() else None


def open_text(path: Path) -> TextIO:
    """Open a UTF-8 text file for reading, plain or archived (see `existing`). A missing
    file raises FileNotFoundError naming the plain path."""
    found = existing(path)
    if found is None:
        raise FileNotFoundError(f"no such file: {path}")
    if found.name.endswith(GZ_SUFFIX):
        return gzip.open(found, "rt", encoding="utf-8")
    return open(found, encoding="utf-8")


def _digest(handle: IO[bytes]) -> str:
    sha = hashlib.sha256()
    while chunk := handle.read(CHUNK_BYTES):
        sha.update(chunk)
    return sha.hexdigest()


def same_content(plain: Path, archived: Path) -> bool:
    """True when the gzip file `archived` holds exactly the bytes of `plain`."""
    with open(plain, "rb") as source, gzip.open(archived, "rb") as packed:
        return _digest(source) == _digest(packed)


def compress(path: Path) -> Path:
    """Replace `path` by `path.gz`: written to a temporary file, fsynced, read back and
    compared byte for byte (SHA-256) before it is renamed into place; only then is the plain
    file removed. Raises OSError (and leaves `path` untouched) on any failure."""
    target = path.with_name(path.name + GZ_SUFFIX)
    temporary = target.with_name(target.name + TMP_SUFFIX)
    with open(path, "rb") as source, open(temporary, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as packed:
            shutil.copyfileobj(source, packed, CHUNK_BYTES)
        raw.flush()
        os.fsync(raw.fileno())
    if not same_content(path, temporary):
        temporary.unlink()
        raise OSError(f"archive of {path} does not read back identical; left as is")
    os.replace(temporary, target)
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)  # the rename is on disk before the plain file goes
    finally:
        os.close(folder)
    path.unlink()
    return target
