"""Append-only JSONL run ledger. Nothing is deleted - old ledgers are archived.

Each line is a complete record of one run (inputs summary, produced count,
artifact, actions, verdict, duration). That makes the ledger an audit trail
AND a ready-made dataset for training or evaluating your own models.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path


class Ledger:
    """Open in append mode only. There is intentionally no delete/update API."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, sort_keys=True)
        if "\n" in line:  # json.dumps never emits raw newlines, but be explicit
            raise ValueError("ledger records must serialise to a single line")
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def rotate_to_archive(self, archive_dir: str | Path | None = None, compress: bool = True) -> Path | None:
        """Move the current ledger into the archive (verified copy first), so the
        live file starts fresh. Returns the archive path, or None if empty."""
        with self._lock:
            return rotate_to_archive(self.path, archive_dir or self.path.parent / "archive", compress)


def _sha256(path: Path, opener=open) -> str:
    h = hashlib.sha256()
    with opener(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _replace_with_retry(src: Path, dst: Path, attempts: int = 100) -> None:
    """os.replace, retried briefly: on Windows a file another writer has open
    cannot be renamed until that writer closes it."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02)


def _unique_dest(archive: Path, src: Path, compress: bool) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ext = f"{src.suffix}{'.gz' if compress else ''}"
    dest = archive / f"{src.stem}-{stamp}{ext}"
    n = 1
    while dest.exists() or dest.with_name(dest.name + ".partial").exists():
        dest = archive / f"{src.stem}-{stamp}-{n}{ext}"
        n += 1
    return dest


def _archive_one(rotating: Path, archive: Path, live: Path, compress: bool) -> Path:
    dest = _unique_dest(archive, live, compress)
    partial = dest.with_name(dest.name + ".partial")
    for _ in range(20):
        size = rotating.stat().st_size
        original = _sha256(rotating)
        try:
            if compress:
                with open(rotating, "rb") as fin, gzip.open(partial, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                copied = _sha256(partial, gzip.open)
            else:
                shutil.copyfile(rotating, partial)
                copied = _sha256(partial)
        except BaseException:
            if partial.exists():
                partial.unlink()  # never leave an unverified partial archive
            raise
        if copied != original:
            partial.unlink()
            raise IOError(f"archive verification failed; records kept in {rotating}")
        if rotating.stat().st_size == size:  # no late writer appended while we copied
            break
        partial.unlink()  # a writer that held the old handle appended: copy again
    else:
        raise IOError(f"{rotating} kept changing; records kept in place")
    os.replace(partial, dest)  # only a verified archive gets its final name
    rotating.unlink()  # safe: a verified copy exists in the archive
    return dest


def rotate_to_archive(path: str | Path, archive_dir: str | Path, compress: bool = True) -> Path | None:
    """Archive-then-trim without losing a record:

    1. atomically rename the live file to `<name>.rotating-<id>`, so every
       append from now on starts a fresh live file;
    2. copy the rotating file into archive_dir as `*.partial`, verify the copy
       byte for byte (sha256) and only then give it its final name;
    3. remove the rotating file.

    If anything fails, the rotating file stays where it is (no data is lost) and
    the next call finishes it. Returns the newest archive path, or None."""
    src = Path(path)
    archive = Path(archive_dir)
    pending = sorted(src.parent.glob(src.name + ".rotating-*"))
    if src.exists() and src.stat().st_size > 0:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        rotating = src.with_name(f"{src.name}.rotating-{stamp}-{uuid.uuid4().hex[:8]}")
        _replace_with_retry(src, rotating)
        pending.append(rotating)
    if not pending:
        return None
    archive.mkdir(parents=True, exist_ok=True)
    dest = None
    for rotating in pending:
        dest = _archive_one(rotating, archive, src, compress)
    return dest
