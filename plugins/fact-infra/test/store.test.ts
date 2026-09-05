/**
 * fact-infra store.ts 核心测试
 */

import { describe, it, before, after } from "node:test";
import * as assert from "node:assert";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import * as store from "../src/store.js";
import * as handlers from "../src/handlers.js";

let tmpDir: string;
let testProjectDir: string;

before(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "fact-test-"));
  process.env.FACT_INDEX_DB = path.join(tmpDir, "index.db");
  process.env.FACT_PROJECTS_ROOT = tmpDir;
  testProjectDir = path.join(tmpDir, "test-proj");
  fs.mkdirSync(testProjectDir, { recursive: true });
});

after(() => {
  if (tmpDir && fs.existsSync(tmpDir)) {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

describe("fact-infra store", () => {
  it("fact_init 建库+注册中央索引", () => {
    const result = JSON.parse(
      handlers.hFactInit({
        project_dir: testProjectDir,
        slug: "test-proj",
        name: "Test Project",
        project_type: "engineering",
      })
    );

    assert.strictEqual(result.ok, true);
    assert.ok(result.db_path);
    assert.ok(fs.existsSync(result.db_path));

    // 验证中央索引
    const idx = store.centralDbPath();
    assert.ok(fs.existsSync(idx));

    const projects = store.rows(idx, "SELECT * FROM project_registry WHERE slug=?", ["test-proj"]);
    assert.strictEqual(projects.length, 1);
    assert.strictEqual(projects[0].slug, "test-proj");
  });

  it("task add/update 生命周期", () => {
    // Add task
    const addResult = JSON.parse(
      handlers.hFactTaskAdd({
        project: testProjectDir,
        title: "Test Task",
        priority: "high",
        notes: "Test notes",
      })
    );
    assert.strictEqual(addResult.ok, true);

    // Query tasks
    const queryResult = JSON.parse(
      handlers.hFactQuery({
        project: testProjectDir,
        table: "tasks",
      })
    );
    assert.strictEqual(queryResult.ok, true);
    assert.strictEqual(queryResult.rows.length, 1);
    assert.strictEqual(queryResult.rows[0].title, "Test Task");
    assert.strictEqual(queryResult.rows[0].status, "todo");

    const taskId = queryResult.rows[0].id;

    // Update task
    const updateResult = JSON.parse(
      handlers.hFactTaskUpdate({
        project: testProjectDir,
        task_id: taskId,
        status: "done",
      })
    );
    assert.strictEqual(updateResult.ok, true);

    // Verify update
    const verifyResult = JSON.parse(
      handlers.hFactQuery({
        project: testProjectDir,
        table: "tasks",
      })
    );
    assert.strictEqual(verifyResult.rows[0].status, "done");
    assert.ok(verifyResult.rows[0].completed_at);
  });

  it("decision add/resolve", () => {
    // Add decision
    const addResult = JSON.parse(
      handlers.hFactDecisionAdd({
        project: testProjectDir,
        title: "Test Decision",
        decision_key: "DEC-001",
        rationale: "Test rationale",
      })
    );
    assert.strictEqual(addResult.ok, true);

    // Resolve decision
    const resolveResult = JSON.parse(
      handlers.hFactDecisionResolve({
        project: testProjectDir,
        decision_key: "DEC-001",
        status: "resolved",
      })
    );
    assert.strictEqual(resolveResult.ok, true);

    // Verify
    const queryResult = JSON.parse(
      handlers.hFactQuery({
        project: testProjectDir,
        table: "decisions",
      })
    );
    assert.strictEqual(queryResult.rows[0].status, "resolved");
  });

  it("issue open/resolve", () => {
    // Open issue
    const openResult = JSON.parse(
      handlers.hFactIssueOpen({
        project: testProjectDir,
        title: "Test Issue",
        issue_key: "ISS-001",
        severity: "high",
      })
    );
    assert.strictEqual(openResult.ok, true);

    // Resolve issue
    const resolveResult = JSON.parse(
      handlers.hFactIssueResolve({
        project: testProjectDir,
        issue_key: "ISS-001",
      })
    );
    assert.strictEqual(resolveResult.ok, true);

    // Verify
    const queryResult = JSON.parse(
      handlers.hFactQuery({
        project: testProjectDir,
        table: "issues",
      })
    );
    assert.strictEqual(queryResult.rows[0].status, "resolved");
    assert.ok(queryResult.rows[0].resolved_at);
  });

  it("exp log", () => {
    const result = JSON.parse(
      handlers.hFactExpLog({
        project: testProjectDir,
        name: "Test Experiment",
        issue_key: "ISS-001",
        input_config: "config1",
        expected_result: "expected1",
        actual_result: "actual1",
        verdict: "conclude",
      })
    );
    assert.strictEqual(result.ok, true);

    const queryResult = JSON.parse(
      handlers.hFactQuery({
        project: testProjectDir,
        table: "experiments",
      })
    );
    assert.strictEqual(queryResult.rows[0].name, "Test Experiment");
    assert.strictEqual(queryResult.rows[0].verdict, "conclude");
  });

  it("journal upsert/query (中央库)", () => {
    // Upsert journal
    const upsertResult = JSON.parse(
      handlers.hFactJournalUpsert({
        name: "Test Journal",
        publisher: "Test Publisher",
        if_value: "5.0",
        jcr_quartile: "Q1",
      })
    );
    assert.strictEqual(upsertResult.ok, true);

    // Query journal
    const queryResult = JSON.parse(
      handlers.hFactJournalQuery({
        name: "Test Journal",
      })
    );
    assert.strictEqual(queryResult.ok, true);
    assert.strictEqual(queryResult.journals.length, 1);
    assert.strictEqual(queryResult.journals[0].name, "Test Journal");
    assert.strictEqual(queryResult.journals[0].if_value, "5.0");
  });

  it("fact_status 计数与 stale 提示", () => {
    const result = JSON.parse(
      handlers.hFactStatus({
        project: testProjectDir,
      })
    );
    assert.strictEqual(result.ok, true);
    assert.ok(result.summary);
    assert.strictEqual(result.summary.tasks, 1);
    assert.strictEqual(result.summary.decisions, 1);
    assert.strictEqual(result.summary.issues, 1);
    assert.strictEqual(result.summary.experiments, 1);
  });

  it("archive → 查询报已归档 → rebind activate 恢复", () => {
    // Archive
    const archiveResult = JSON.parse(
      handlers.hFactArchive({
        project: testProjectDir,
        force: true,
      })
    );
    assert.strictEqual(archiveResult.ok, true);

    // Try to query archived project
    const queryResult = JSON.parse(
      handlers.hFactStatus({
        project: "test-proj",
      })
    );
    assert.ok(queryResult.error);
    assert.ok(queryResult.error.includes("已归档"));

    // Rebind with activate
    const rebindResult = JSON.parse(
      handlers.hFactRebind({
        project: "test-proj",
        activate: true,
      })
    );
    assert.strictEqual(rebindResult.ok, true);

    // Verify reactivated
    const statusResult = JSON.parse(
      handlers.hFactStatus({
        project: "test-proj",
      })
    );
    assert.strictEqual(statusResult.ok, true);
  });

  it("schema 迁移: 手工造 v1 库打开后自动升 v2", async () => {
    const v1Dir = path.join(tmpDir, "v1-proj");
    fs.mkdirSync(v1Dir, { recursive: true });
    const v1DbPath = path.join(v1Dir, "fact.db");

    // Create v1 schema manually
    const { DatabaseSync } = await import("node:sqlite");
    const conn = new DatabaseSync(v1DbPath);
    conn.exec("PRAGMA user_version = 1");
    conn.exec("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)");
    conn.exec("INSERT INTO meta(key, value) VALUES('schema_version', '1')");
    conn.exec("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, status TEXT DEFAULT 'todo')");
    conn.exec("CREATE TABLE decisions (id INTEGER PRIMARY KEY, title TEXT)");
    conn.exec("CREATE TABLE issues (id INTEGER PRIMARY KEY, title TEXT)");
    conn.exec("CREATE TABLE experiments (id INTEGER PRIMARY KEY, name TEXT)");
    conn.close();

    // Open with store (should trigger migration)
    const dbPath = store.initProjectDb(v1Dir, "v1-proj");
    assert.ok(fs.existsSync(dbPath));

    // Verify v2 schema
    const rows = store.rows(dbPath, "SELECT name FROM sqlite_master WHERE type='table' AND name='goals'");
    assert.strictEqual(rows.length, 1);

    const eventsRows = store.rows(dbPath, "SELECT name FROM sqlite_master WHERE type='table' AND name='events'");
    assert.strictEqual(eventsRows.length, 1);
  });

  it("fact_query 白名单拒绝非法表名", () => {
    const result = JSON.parse(
      handlers.hFactQuery({
        project: testProjectDir,
        table: "malicious_table",
      })
    );
    assert.ok(result.error);
    assert.ok(result.error.includes("未知表"));
  });

  it("resolve_project_db 回退链(slug/路径/不存在)", () => {
    // Test slug resolution
    const resolved1 = store.resolveProjectDb("test-proj");
    assert.ok(resolved1);
    assert.ok(resolved1.toString().endsWith("fact.db"));

    // Test path resolution
    const resolved2 = store.resolveProjectDb(testProjectDir);
    assert.ok(resolved2);

    // Test non-existent
    const resolved3 = store.resolveProjectDb("non-existent-proj");
    assert.strictEqual(resolved3, null);
  });

  it("task_update 不存在的 task_id 报错", () => {
    const result = JSON.parse(
      handlers.hFactTaskUpdate({
        project: testProjectDir,
        task_id: 99999,
        status: "done",
      })
    );
    assert.ok(result.error);
    assert.ok(result.error.includes("task_id 不存在") || result.error.includes("99999"));
  });

  it("decision_resolve 不存在的 decision_key 报错", () => {
    const result = JSON.parse(
      handlers.hFactDecisionResolve({
        project: testProjectDir,
        decision_key: "NONEXIST",
      })
    );
    assert.ok(result.error);
    assert.ok(result.error.includes("decision_key 不存在") || result.error.includes("NONEXIST"));
  });

  it("issue_resolve 不存在的 issue_key 报错", () => {
    const result = JSON.parse(
      handlers.hFactIssueResolve({
        project: testProjectDir,
        issue_key: "NONEXIST",
      })
    );
    assert.ok(result.error);
    assert.ok(result.error.includes("issue_key 不存在") || result.error.includes("NONEXIST"));
  });

  it("goal add 和 query detail", () => {
    const addResult = JSON.parse(
      handlers.hFactGoalAdd({
        project: testProjectDir,
        title: "Test Goal",
        done_criteria: "Complete when X",
        phase: "建档",
      })
    );
    assert.strictEqual(addResult.ok, true);
    const goalId = addResult.goal_id;

    // Query goal detail
    const queryResult = JSON.parse(
      handlers.hFactQuery({
        project: testProjectDir,
        goal_id: goalId,
      })
    );
    assert.strictEqual(queryResult.ok, true);
    assert.strictEqual(queryResult.goal.title, "Test Goal");
  });

  it("phase_set 守卫：active goals 需 confirm", () => {
    // Without confirm
    const result1 = JSON.parse(
      handlers.hFactPhaseSet({
        project: testProjectDir,
        phase: "验证",
      })
    );
    assert.ok(result1.error);
    assert.ok(result1.error.includes("active") || result1.error.includes("confirm"));

    // With confirm
    const result2 = JSON.parse(
      handlers.hFactPhaseSet({
        project: testProjectDir,
        phase: "验证",
        confirm: true,
      })
    );
    assert.strictEqual(result2.ok, true);
    assert.strictEqual(result2.phase, "验证");
  });

  it("goal_update 闭环检查：未收尾项需 confirm", () => {
    // Create goal with linked task
    const goalResult = JSON.parse(
      handlers.hFactGoalAdd({
        project: testProjectDir,
        title: "Goal with Task",
        done_criteria: "Done when task complete",
      })
    );
    const goalId = goalResult.goal_id;

    // Link a task to this goal
    const dbPath = store.resolveProjectDb(testProjectDir) as string;
    store.exec(dbPath, "INSERT INTO tasks(title, goal_id, status) VALUES(?,?,?)", ["Linked Task", goalId, "todo"]);

    // Try to mark goal as done without confirm
    const updateResult1 = JSON.parse(
      handlers.hFactGoalUpdate({
        project: testProjectDir,
        goal_id: goalId,
        status: "done",
      })
    );
    assert.ok(updateResult1.error);
    assert.ok(updateResult1.error.includes("未收尾") || updateResult1.error.includes("confirm"));

    // With confirm
    const updateResult2 = JSON.parse(
      handlers.hFactGoalUpdate({
        project: testProjectDir,
        goal_id: goalId,
        status: "done",
        confirm: true,
      })
    );
    assert.strictEqual(updateResult2.ok, true);
  });

  it("note_add refs 验证", () => {
    // Valid refs
    const result1 = JSON.parse(
      handlers.hFactNoteAdd({
        project: testProjectDir,
        text: "Test note",
        refs: JSON.stringify([{ type: "file", value: "test.md" }]),
      })
    );
    assert.strictEqual(result1.ok, true);

    // Invalid refs type
    const result2 = JSON.parse(
      handlers.hFactNoteAdd({
        project: testProjectDir,
        text: "Test note 2",
        refs: JSON.stringify([{ type: "invalid", value: "x" }]),
      })
    );
    assert.ok(result2.error);
    assert.ok(result2.error.includes("越界"));
  });
});
