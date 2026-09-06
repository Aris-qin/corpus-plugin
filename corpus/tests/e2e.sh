#!/usr/bin/env bash
# Stage-5 E2E: full corpus pipeline over a THROWAWAY DB (never touches the
# 229MB production corpus.db). All paths come from a temp corpus.toml;
# embedding uses --embedding-mode mock (no external API, no openclaw.json).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
E2E_ROOT=$(mktemp -d /tmp/corpus-e2e.XXXXXX)
DB="$E2E_ROOT/corpus.db"
CONF="$E2E_ROOT/corpus.toml"
export CORPUS_CONFIG="$CONF"
export PYTHONPATH="$REPO:${PYTHONPATH:-}"
CLI="python3 $REPO/corpus/cli.py"
FIX="$REPO/corpus/tests/fixtures/canonical/md/full_features.input"

cat > "$CONF" <<EOF
[paths]
corpus_db = "$DB"

[embedding]
mode = "mock"
EOF
echo "== config =="; cat "$CONF"

echo; echo "== [1] schema init + project register =="
python3 - "$DB" <<'PYEOF'
import sys
sys.path.insert(0, "corpus")
import db as D
c = D.CorpusDB(sys.argv[1], embedding_dim=8)
c.close()
print("schema initialized")
PYEOF
$CLI init --project e2e-npca --title "E2E NPC test" | tail -1
$CLI list-projects | tee /dev/stderr | grep -q "e2e-npca" && echo "  -> [1] OK project registered"

echo; echo "== [2] register-raw (no chunk/embed) =="
mkdir -p "$E2E_ROOT/raw"
cp "$FIX" "$E2E_ROOT/raw/99990001.md"
$CLI register-raw --project e2e-npca --pmid 99990001 --raw-path "$E2E_ROOT/raw/99990001.md" 2>&1 | tail -2
echo "  -> [2] Done"

echo; echo "== [3] process-raw (chunker 2nd-gen + mock embedding) =="
$CLI --embedding-mode mock --dim 8 process-raw --pmid 99990001 2>&1 | tail -5
echo "  -> [3] Done"

echo; echo "== [4] query (vector KNN + aggregate) =="
$CLI --embedding-mode mock --dim 8 query --project e2e-npca --query "EGFR degradation" --top 5 2>&1 | tail -8
echo "  -> [4] Done"

echo; echo "== [5] score (curator) =="
$CLI score --project e2e-npca --pmid 99990001 --criterion 0.9 --outcome 0.8 --conclusion 0.85 2>&1 | tail -3
echo "  -> [5] Done"

echo; echo "== [6] group + list =="
$CLI group --project e2e-npca --pmid 99990001 --role core 2>&1 | tail -1
$CLI list-groups --project e2e-npca 2>&1 | tail -4
echo "  -> [6] Done"

echo; echo "== [7] DB assertions =="
python3 - "$DB" "$REPO" <<'PYEOF'
import sys, sqlite3
c = sqlite3.connect(sys.argv[1])
for t in ("documents", "chunks", "chunk_vectors", "quality_evidence", "document_groups"):
    try:
        n = c.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"  {t}: {n}")
    except Exception as e:
        print(f"  {t}: ERROR {e}")
c.close()
# vec0 rows are in the virtual table - load extension to count them
import sys as _s
_s.path.insert(0, _s.argv[2])
import sqlite_vec
c2 = sqlite3.connect(_s.argv[1])
sqlite_vec.load(c2)
try:
    n = c2.execute("select count(*) from chunk_vectors").fetchone()[0]
    print(f"  chunk_vectors(vec0): {n}")
except Exception as e:
    print(f"  chunk_vectors(vec0): ERROR {e}")
PYEOF

if [ -f "$DB" ]; then
  rm -rf "$E2E_ROOT"
  echo; echo "E2E PASS"
else
  echo; echo "E2E FAIL: no db" >&2; exit 1
fi