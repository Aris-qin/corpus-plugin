#!/usr/bin/env python3
"""corpus.backup — consistent SQLite snapshot + verification (IMPLEMENTATION.md §4).

A backup is a hot snapshot taken through the SQLite backup API (never a file copy
of a live WAL database). The timestamp is produced by ``datetime.now(timezone.utc)``
— NEVER a literal ``$(date)`` shell substitution (that was the last-round bug: the
filename ended up containing the eight literal characters ``$(date)``).

Sequence:
  1. open the source read-only,
  2. ``PRAGMA wal_checkpoint(PASSIVE)`` to fold committed WAL frames in,
  3. ``.backup()`` into a fresh destination,
  4. ``PRAGMA integrity_check`` on the destination — anything but ``ok`` fails.

On any ``sqlite3.Error`` / ``OSError`` (e.g. ``ENOSPC``) the unverified destination
is removed and a clear error is raised, so an aborted/rerun backup never leaves a
half-written target behind.

CLI: ``python -m corpus.backup <src.sqlite> <backup_dir>`` prints the snapshot path.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


class BackupError(RuntimeError):
    """Backup failed and any partial target was cleaned up."""


def utc_stamp() -> str:
    """``YYYYMMDDTHHMMSSZ`` UTC — no shell substitution, ever."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_filename(src_path: Path, stamp: str | None = None) -> str:
    """``<stem>.bak-<utcstamp>.sqlite``. Guaranteed free of literal ``$(date)``."""
    stamp = stamp or utc_stamp()
    return f"{Path(src_path).stem}.bak-{stamp}.sqlite"


def backup(src_path: str | Path, backup_dir: str | Path,
           *, stamp: str | None = None, pages: int = 4096, sleep: float = 0.1) -> Path:
    """Take a verified snapshot of ``src_path`` into ``backup_dir``; return its path.

    ``src_path`` is opened read-only so concurrent readers/writers keep working and
    the source is never mutated. Raises ``BackupError`` on failure (partial target
    removed)."""
    src_path = Path(src_path).resolve()
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst_path = backup_dir / backup_filename(src_path, stamp)

    if dst_path.exists():
        # A rerun must not silently overwrite; verify the existing target instead.
        if _integrity_ok(dst_path):
            return dst_path
        dst_path.unlink()

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.execute("PRAGMA wal_checkpoint(PASSIVE)")
        src.backup(dst, pages=pages, sleep=sleep)
        dst.commit()
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("backup integrity_check failed")
    except (sqlite3.Error, OSError) as e:
        dst.close()
        src.close()
        _cleanup(dst_path)
        raise BackupError(f"backup failed ({e!r}); partial target removed: {dst_path}") from e
    else:
        dst.close()
        src.close()
        return dst_path


def _integrity_ok(db_path: Path) -> bool:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _cleanup(dst_path: Path) -> None:
    try:
        if dst_path.exists():
            dst_path.unlink()
    except OSError:
        pass


def restore_check(db_path: str | Path, *, enable_foreign_keys: bool = True) -> dict:
    """Post-restore verification gate: ``integrity_check`` + ``foreign_key_check``.

    Returns ``{"integrity": "ok"|..., "foreign_key_violations": N}``. The worker
    health gate refuses to accept writes unless integrity is ``ok`` and there are
    zero FK violations (vec0 fsck is wired in stage 5)."""
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        if enable_foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        return {"integrity": integrity, "foreign_key_violations": fk_violations,
                "ok": integrity == "ok" and fk_violations == 0}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python -m corpus.backup <src.sqlite> <backup_dir>", file=sys.stderr)
        return 2
    dst = backup(argv[0], argv[1])
    print(dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
