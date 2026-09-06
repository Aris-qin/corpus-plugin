#!/usr/bin/env bash
# Stage-5 plugin smoke: fact-infra — register all 19 tools, assert they expose
# execute, then run a representative real call chain against a throwaway DB.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SMOKE=$(mktemp -d /tmp/fi-smoke.XXXXXX)
export FACT_PROJECTS_ROOT="$SMOKE/projects"
export REPO="$REPO"
mkdir -p "$FACT_PROJECTS_ROOT/demo"  # fact-infra requires the dir to already exist

DRIVER="$SMOKE/drive.mjs"
cat > "$DRIVER" <<'PSEOF'
import { getToolPluginMetadata } from "/app/dist/plugin-sdk/tool-plugin.js";
const mod = await import(process.env.REPO + "/plugins/fact-infra/dist/src/index.js");
const entry = mod.default;
const meta = getToolPluginMetadata(entry);
if (!meta) { console.log("FAIL: no tool metadata"); process.exit(1); }

const registered = {};
const mockApi = { registerTool(tool, opts) { registered[tool.name] = { ...tool, opts }; } };
entry.register(mockApi);

const names = meta.tools.map(t => t.name).sort();
console.log("metadata tools:", names.length);
console.log("registered tools:", Object.keys(registered).length);
if (names.length !== 19 || Object.keys(registered).length !== 19) {
  console.log("FAIL: expected 19 tools");
  process.exit(1);
}
const missing = meta.tools.filter(t => typeof registered[t.name]?.execute !== "function");
console.log("missing execute:", missing.length);
if (missing.length) { console.log(missing.map(m => m.name)); process.exit(1); }
console.log("all 19 tools registered + executable");

// representative chain: init -> status -> note -> task -> decision -> issue -> goal -> query
const P = (p) => `${process.env.FACT_PROJECTS_ROOT}/${p}`;
const chain = [
  ["fact_init", { project_dir: P("demo"), slug: "demo", name: "Demo", project_type: "paper" }, "init"],
  ["fact_status", { project: P("demo") }, "status"],
  ["fact_note_add", { project: P("demo"), text: "e2e note", refs: [] }, "note"],
  ["fact_task_add", { project: P("demo"), title: "verify smoke" }, "task"],
  ["fact_decision_add", { project: P("demo"), title: "decide A over B" }, "decision"],
  ["fact_issue_open", { project: P("demo"), title: "found a smell" }, "issue"],
  ["fact_goal_add", { project: P("demo"), title: "finish stage5", done_criteria: "smoke green" }, "goal"],
  ["fact_query", { project: P("demo"), table: "tasks" }, "query tasks"],
];
let pass = 0, fail = 0;
for (const [name, params, label] of chain) {
  try {
    const raw = await registered[name].execute("call-1", params, undefined, undefined);
    const out = typeof raw === "string" ? raw : JSON.stringify(raw);
    const obj = JSON.parse(typeof raw === "string" ? raw : (raw.content?.[0]?.text ?? raw));
    const ok = obj?.ok === true || obj?.error === undefined;
    console.log(`  [${label}] ${name} -> ok=${ok} ${out.slice(0, 110)}`);
    ok ? pass++ : fail++;
  } catch (e) {
    console.log(`  [${label}] ${name} -> FAIL: ${e.message.slice(0, 140)}`);
    fail++;
  }
}
console.log(`\nRESULT: ${pass} passed, ${fail} failed (of ${chain.length})`);
process.exit(fail ? 1 : 0);
PSEOF

timeout 90 node "$DRIVER" 2>&1
RC=$?
rm -rf "$SMOKE"
echo "fact-infra SMOKE exit=$RC"