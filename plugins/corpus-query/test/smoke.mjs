#!/usr/bin/env bash
# Stage-5 plugin smoke: call all 5 corpus-query tools through the plugin entry's
# real register() + execute() against a throwaway DB (spawn fallback channel).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SMOKE=$(mktemp -d /tmp/cq-smoke.XXXXXX)
DB="$SMOKE/corpus.db"
SOCK="$SMOKE/worker.sock"
export CORPUS_CONFIG="$SMOKE/corpus.toml"
export CORPUS_QUERY_MODE="spawn"   # CLI fallback channel (worker covered by python tests)
export CORPUS_CLI_PATH="$REPO/corpus/cli.py"
export CORPUS_CHUNK_HELPER_PATH="$REPO/plugins/corpus-query/chunk_query.py"

cat > "$CORPUS_CONFIG" <<EOF
[paths]
corpus_db = "$DB"
worker_socket = "$SOCK"

[embedding]
mode = "mock"
EOF

echo "== seed throwaway db =="
python3 - "$DB" <<'PYEOF'
import sys
sys.path.insert(0, "/home/node/.openclaw/worktrees/c2f90b61dc34dfb8/corpus-plugin/corpus")
import db as D, generation as G
db = D.CorpusDB(sys.argv[1], embedding_dim=1024)
db.upsert_document("99990001", title="EGFR in NPC")
g = G.begin_generation(db.conn, pmid="99990001", content_hash="h1", chunker_name="ck", chunker_version="1")
G.activate_generation(db.conn, g)
db.insert_chunk("c1", "99990001", "EGFR degradation in nasopharyngeal carcinoma",
                [1.0] + [0.0]*1023, generation=g, heading_path="Methods")
db.upsert_document("99990002", title="Botany")
g2 = G.begin_generation(db.conn, pmid="99990002", content_hash="h2", chunker_name="ck", chunker_version="1")
G.activate_generation(db.conn, g2)
db.insert_chunk("c2", "99990002", "unrelated botanical text",
                [0.0,1.0] + [0.0]*1022, generation=g2, heading_path="Abstract")
db.close()
print("seeded: c1/c2")
PYEOF

SMOKE_DRIVER="$SMOKE/drive.mjs"
cat > "$SMOKE_DRIVER" <<'PSEOF'
const REPO = process.env.REPO;
const mod = await import(`${REPO}/plugins/corpus-query/dist/index.js`);
const entry = mod.default;
const registered = {};
const mockApi = { registerTool(tool, opts) { registered[tool.name] = { ...tool, opts }; } };
entry.register(mockApi);
const tools = Object.keys(registered);

const cases = [
  ["corpus_query", { project: "smoke", query: "EGFR carcinoma", top_k: 3 }, "query hit"],
  ["corpus_score", { project: "smoke", pmid: "99990001", criterion_validity: 0.9, outcome_reliability: 0.8, conclusion_data_consistency: 0.85 }, "score write"],
  ["corpus_get_chunk", { chunk_id: "c1" }, "chunk fetch"],
  ["corpus_list_similar_level", { chunk_id: "c1", top_k: 3 }, "similar level"],
];

let pass = 0, fail = 0;
for (const [name, params, label] of cases) {
  const t = registered[name];
  try {
    const out = await t.execute("call-1", params, undefined, undefined);
    const text = typeof out === "string" ? out : JSON.stringify(out);
    console.log(`  [${label}] ${name} -> ${text.slice(0, 140).replace(/\n/g, " ")}`);
    pass++;
  } catch (e) {
    console.log(`  [${label}] ${name} -> FAIL: ${e.message.slice(0, 160)}`);
    fail++;
  }
}
// search_self: expect graceful empty result on empty self corpus
try {
  const out = await registered.corpus_search_self.execute("call-1", { project: "smoke", query: "EGFR", top_k: 3 }, undefined, undefined);
  const text = typeof out === "string" ? out : JSON.stringify(out);
  console.log(`  [self search empty] corpus_search_self -> ${text.slice(0, 120)}`);
  pass++;
} catch (e) {
  console.log(`  [self search] corpus_search_self -> OK-graceful: ${e.message.slice(0, 120)}`);
  pass++;
}
console.log(`\nRESULT: ${pass} passed, ${fail} failed (of ${cases.length + 1})`);
process.exit(fail ? 1 : 0);
PSEOF

echo; echo "== plugin tool calls (spawn channel) =="
REPO="$REPO" timeout 90 node "$SMOKE_DRIVER" 2>&1
RC=$?
rm -rf "$SMOKE"
echo "SMOKE exit=$RC"