"""V2 schema migration for chunks table.

Run: python3 migrate_v2.py [--db /path/to/corpus.db]

Adds V2 tree fields:
  level INTEGER (0=preface, 1/2/3=heading depth)
  path TEXT ('/ch5/5.3/5.3.1')
  parent_id TEXT
  child_ids TEXT (JSON array)
  sibling_ids TEXT (JSON array)
  heading_chain TEXT (JSON array)
  doc_id TEXT

Also creates 3 indexes for tree traversal:
  idx_chunks_path (prefix-match queries)
  idx_chunks_parent (child-of queries)
  idx_chunks_doc (group-by source)
"""
import sqlite3
import sys
from pathlib import Path

V2_COLUMNS = [
    ("level", "INTEGER DEFAULT 0"),
    ("path", "TEXT"),
    ("parent_id", "TEXT"),
    ("child_ids", "TEXT"),       # JSON array string
    ("sibling_ids", "TEXT"),     # JSON array string
    ("heading_chain", "TEXT"),   # JSON array string
    ("doc_id", "TEXT"),
]

V2_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)",
]


def has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def migrate(db_path: str) -> None:
    p = Path(db_path)
    if not p.exists():
        print(f"ERROR: db not found: {p}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(p))
    print(f"[migrate] db: {p}")

    added = 0
    for col_name, col_def in V2_COLUMNS:
        if has_column(conn, "chunks", col_name):
            print(f"  ✓ already has column: {col_name}")
        else:
            sql = f"ALTER TABLE chunks ADD COLUMN {col_name} {col_def}"
            conn.execute(sql)
            print(f"  + added column: {col_name} {col_def}")
            added += 1

    for idx_sql in V2_INDEXES:
        conn.execute(idx_sql)
        idx_name = idx_sql.split("idx_")[1].split(" ")[0]
        print(f"  + index: idx_{idx_name}")

    conn.commit()
    print(f"\n[migrate] done: {added} columns added, {len(V2_INDEXES)} indexes ensured")

    # verify
    print("\n[migrate] chunks schema now:")
    rows = conn.execute("PRAGMA table_info(chunks)").fetchall()
    for r in rows:
        marker = " (V2)" if r[1] in [c[0] for c in V2_COLUMNS] else ""
        print(f"  {r[0]:2d} {r[1]:18s} {r[2]:15s}{marker}")

    conn.close()


if __name__ == "__main__":
    db = "/root/.openclaw/workspace/projects/_corpus/corpus.db"
    if len(sys.argv) > 2 and sys.argv[1] == "--db":
        db = sys.argv[2]
    migrate(db)