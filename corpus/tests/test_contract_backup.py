"""Contract tests for corpus.backup (IMPLEMENTATION.md §4).

Covers: filename carries a real UTC timestamp and never the literal ``$(date)``;
a consistent snapshot is produced and passes integrity/FK checks; a concurrent
reader keeps working while the snapshot is taken; a mid-backup failure (e.g.
ENOSPC) removes the unverified target and raises; a rerun reuses a valid target
instead of overwriting it.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

import backup as B


def _make_source(path, rows=50):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def test_filename_has_no_dollar_date():
    name = B.backup_filename("corpus.db")
    assert "$(date)" not in name
    assert "$" not in name
    assert re.fullmatch(r"corpus\.bak-\d{8}T\d{6}Z\.sqlite", name)


def test_backup_produces_verified_snapshot(tmp_path):
    src = _make_source(tmp_path / "corpus.db")
    dst = B.backup(src, tmp_path / "backups")
    assert dst.exists()
    assert "$(date)" not in dst.name
    check = B.restore_check(dst)
    assert check["ok"] is True
    # snapshot has the same rows as the source
    n = sqlite3.connect(str(dst)).execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n == 50


def test_concurrent_reader_snapshot_opens(tmp_path):
    src = _make_source(tmp_path / "corpus.db")
    reader = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        # a reader is mid-transaction while we snapshot
        assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
        dst = B.backup(src, tmp_path / "backups")
        assert B.restore_check(dst)["ok"] is True
        # reader still works afterwards
        assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    finally:
        reader.close()


def test_disk_full_removes_partial_target_and_raises(tmp_path, monkeypatch):
    src = _make_source(tmp_path / "corpus.db")

    _orig_connect = B.sqlite3.connect

    class _Boom:
        """Proxy whose .backup raises ENOSPC; everything else delegates."""
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def backup(self, *a, **k):
            raise OSError(28, "No space left on device")

        def __getattr__(self, name):
            return getattr(self._real, name)

    def fake_connect(target, *a, **k):
        real = _orig_connect(target, *a, **k)
        # wrap the *source* (read-only) connection so src.backup(dst) raises
        if "mode=ro" in str(target):
            return _Boom(real)
        return real

    monkeypatch.setattr(B.sqlite3, "connect", fake_connect)

    backup_dir = tmp_path / "backups"
    with pytest.raises(B.BackupError):
        B.backup(src, backup_dir)
    # the unverified target must have been cleaned up
    assert list(backup_dir.glob("*.sqlite")) == []


def test_rerun_reuses_valid_target(tmp_path):
    src = _make_source(tmp_path / "corpus.db")
    stamp = "20260906T000000Z"
    d1 = B.backup(src, tmp_path / "backups", stamp=stamp)
    mtime1 = d1.stat().st_mtime_ns
    d2 = B.backup(src, tmp_path / "backups", stamp=stamp)
    assert d1 == d2
    assert d2.stat().st_mtime_ns == mtime1        # not rewritten
