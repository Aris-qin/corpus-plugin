/**
 * fact-infra: SQLite storage layer for the fact infrastructure plugin.
 *
 * Layout (per DESIGN.md):
 * - Project fact.db:  <project_dir>/fact.db   -- per-project source of truth
 * - Central index:    ~/.openclaw/fact-index.db -- journals + project registry
 *
 * Schema v2. `PRAGMA user_version` is the migration authority; the historical
 * `meta.schema_version` value is retained only for compatibility.
 */

import { DatabaseSync } from "node:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";
import { homedir } from "node:os";

export const SCHEMA_VERSION = 2;

export const PHASE_TEMPLATES: Record<string, string[]> = {
  engineering: ["建档", "git", "产品设计", "技术方案", "迭代生成", "验证"],
  paper: ["选题", "检索", "大纲", "初稿", "投稿", "修回", "发表"],
  review: ["选题", "检索", "综合大纲", "撰写", "定稿"],
  revision: ["意见分析", "补实验", "写作", "提交"],
  grant: ["方向确定", "本子撰写", "形式审查", "函评", "会评", "提交"],
  patent: ["交底", "撰写", "提交", "答复"],
  clinical: ["建档", "方案", "迭代生成", "验证"],
  infra: ["建档", "方案", "迭代生成", "验证"],
};

export class MigrationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MigrationError";
  }
}

export class ArchivedProjectPath extends String {
  constructor(path: string) {
    super(path);
  }
}

// ---------------------------------------------------------------------------
// Schema DDL
// ---------------------------------------------------------------------------

const PROJECT_TABLES: Record<string, string> = {
  meta: `
    CREATE TABLE IF NOT EXISTS meta (
      key   TEXT PRIMARY KEY,
      value TEXT
    )
  `,
  tasks: `
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      type TEXT DEFAULT 'task',
      status TEXT DEFAULT 'todo',
      priority TEXT DEFAULT 'medium',
      notes TEXT,
      goal_id INTEGER,
      completed_at TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    )
  `,
  decisions: `
    CREATE TABLE IF NOT EXISTS decisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      decision_key TEXT UNIQUE,
      title TEXT NOT NULL,
      status TEXT DEFAULT 'active',
      summary TEXT,
      rationale TEXT,
      source_file TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    )
  `,
  issues: `
    CREATE TABLE IF NOT EXISTS issues (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      issue_key TEXT UNIQUE,
      title TEXT NOT NULL,
      status TEXT DEFAULT 'open',
      severity TEXT DEFAULT 'medium',
      hypothesis TEXT,
      workaround TEXT,
      goal_id INTEGER,
      resolved_at TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    )
  `,
  experiments: `
    CREATE TABLE IF NOT EXISTS experiments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      issue_key TEXT,
      name TEXT NOT NULL,
      input_config TEXT,
      expected_result TEXT,
      actual_result TEXT,
      verdict TEXT DEFAULT 'pending',
      run_at TEXT,
      notes TEXT,
      goal_id INTEGER,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    )
  `,
  goals: `
    CREATE TABLE IF NOT EXISTS goals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      done_criteria TEXT,
      status TEXT NOT NULL DEFAULT 'active'
             CHECK (status IN ('active','done','abandoned')),
      parent_goal_id INTEGER,
      phase TEXT,
      origin TEXT NOT NULL DEFAULT 'normal'
             CHECK (origin IN ('normal','backfilled')),
      position INTEGER,
      created_at TEXT DEFAULT (datetime('now')),
      completed_at TEXT
    )
  `,
  events: `
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      goal_id INTEGER,
      phase TEXT,
      text TEXT NOT NULL,
      refs TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    )
  `,
};

const PROJECT_V2_INDEXES = [
  "CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status)",
  "CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_goal_id)",
  "CREATE INDEX IF NOT EXISTS idx_events_goal ON events(goal_id)",
  "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)",
];

const CENTRAL_TABLES: Record<string, string> = {
  journals: `
    CREATE TABLE IF NOT EXISTS journals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      publisher TEXT, abbrev TEXT, issn TEXT,
      if_year TEXT, if_value TEXT, jcr_quartile TEXT,
      abstract_format TEXT, abstract_max_words INTEGER,
      abstract_required_headers TEXT,
      imrd_required INTEGER, prisma_abstract_recommended INTEGER,
      reporting_checklist_required INTEGER,
      detailed_search_strategies_required INTEGER,
      word_limit_main INTEGER, word_over_limit_fee INTEGER,
      registration_recommended TEXT, narrative_review_policy TEXT,
      manuscript_structure TEXT, title_format_recommendation TEXT,
      keywords_count_min INTEGER, keywords_count_max INTEGER,
      mesh_keywords_recommended INTEGER,
      submission_url TEXT, source_url TEXT,
      notes TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    )
  `,
  project_registry: `
    CREATE TABLE IF NOT EXISTS project_registry (
      slug TEXT PRIMARY KEY,
      db_path TEXT NOT NULL,
      name TEXT,
      status TEXT DEFAULT 'active',
      summary TEXT,
      target_journal TEXT,
      last_activity_at TEXT,
      updated_at TEXT,
      category TEXT,
      archived_at TEXT
    )
  `,
};

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

export function centralDbPath(): string {
  const override = process.env.FACT_INDEX_DB;
  if (override) {
    return override;
  }
  const stateDir = process.env.OPENCLAW_STATE_DIR || path.join(homedir(), ".openclaw");
  return path.join(stateDir, "fact-index.db");
}

export function projectDbPath(projectDir: string): string {
  return path.join(projectDir, "fact.db");
}

export function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function parseTimestampUtc(ts: string): Date | null {
  if (!ts || typeof ts !== "string") {
    return null;
  }
  ts = ts.trim();

  // ISO 8601 with Z
  if (ts.endsWith("Z")) {
    try {
      return new Date(ts);
    } catch {
      return null;
    }
  }

  // ISO 8601 with offset
  if (ts.includes("T") && (ts.includes("+") || ts.split("-").length > 3)) {
    try {
      return new Date(ts);
    } catch {
      return null;
    }
  }

  // SQLite naive format (interpret as UTC)
  if (ts.includes(" ") && !ts.includes("T")) {
    try {
      return new Date(ts + "Z");
    } catch {
      return null;
    }
  }

  return null;
}

// ---------------------------------------------------------------------------
// Connection helpers
// ---------------------------------------------------------------------------

const connections = new Map<string, DatabaseSync>();

function getConn(dbPath: string): DatabaseSync {
  let conn = connections.get(dbPath);
  if (!conn) {
    if (!fs.existsSync(dbPath)) {
      throw new Error(`fact.db 不存在: ${dbPath}`);
    }
    const stats = fs.statSync(dbPath);
    if (stats.size === 0) {
      throw new Error(`fact.db 是空文件（0 字节），无法读取: ${dbPath}`);
    }
    // timeout parameter is busy_timeout in ms (30s)
    conn = new DatabaseSync(dbPath, { open: true });
    conn.exec("PRAGMA journal_mode=WAL");
    conn.exec("PRAGMA busy_timeout=30000");
    connections.set(dbPath, conn);
  }
  return conn;
}

export function exec(dbPath: string, sql: string, params: any[] = []): void {
  const conn = getConn(dbPath);
  const stmt = conn.prepare(sql);
  stmt.run(...params);
}

export function execChange(dbPath: string, sql: string, params: any[] = []): number {
  const conn = getConn(dbPath);
  const stmt = conn.prepare(sql);
  const info = stmt.run(...params);
  return typeof info.changes === 'bigint' ? Number(info.changes) : info.changes;
}

export function rows<T = any>(dbPath: string, sql: string, params: any[] = []): T[] {
  const conn = getConn(dbPath);
  const stmt = conn.prepare(sql);
  return stmt.all(...params) as T[];
}

// ---------------------------------------------------------------------------
// Init / schema
// ---------------------------------------------------------------------------

function tableNames(conn: DatabaseSync): Set<string> {
  const stmt = conn.prepare("SELECT name FROM sqlite_master WHERE type='table'");
  const result = stmt.all() as Array<{ name: string }>;
  return new Set(result.map((r) => r.name));
}

function readUserVersion(conn: DatabaseSync): number {
  const stmt = conn.prepare("PRAGMA user_version");
  const result = stmt.get() as { user_version: number };
  return result.user_version;
}

function setUserVersion(conn: DatabaseSync, version: number): void {
  conn.exec(`PRAGMA user_version = ${version}`);
}

function addColumnIfMissing(conn: DatabaseSync, table: string, column: string, ddl: string): void {
  const stmt = conn.prepare(`PRAGMA table_info(${table})`);
  const columns = stmt.all() as Array<{ name: string }>;
  const columnSet = new Set(columns.map((c) => c.name));
  if (!columnSet.has(column)) {
    conn.exec(`ALTER TABLE ${table} ADD COLUMN ${ddl}`);
  }
}

function createProjectV2(conn: DatabaseSync): void {
  for (const ddl of Object.values(PROJECT_TABLES)) {
    conn.exec(ddl);
  }
  for (const ddl of PROJECT_V2_INDEXES) {
    conn.exec(ddl);
  }
}

function step1To1_1(conn: DatabaseSync): void {
  const stmt = conn.prepare("PRAGMA table_info(experiments)");
  const columns = stmt.all() as Array<{ name: string }>;
  const cols = new Set(columns.map((c) => c.name));
  if (!cols.has("updated_at")) {
    conn.exec("ALTER TABLE experiments ADD COLUMN updated_at TEXT");
    conn.exec("UPDATE experiments SET updated_at = datetime('now') WHERE updated_at IS NULL OR updated_at = ''");
  }
}

function step1_1To2(conn: DatabaseSync): void {
  conn.exec(PROJECT_TABLES.goals);
  conn.exec(PROJECT_TABLES.events);
  for (const ddl of PROJECT_V2_INDEXES) {
    conn.exec(ddl);
  }
  addColumnIfMissing(conn, "tasks", "goal_id", "goal_id INTEGER");
  addColumnIfMissing(conn, "issues", "goal_id", "goal_id INTEGER");
  addColumnIfMissing(conn, "experiments", "goal_id", "goal_id INTEGER");
}

function projectRowCounts(conn: DatabaseSync): Record<string, number> {
  const tables = ["tasks", "decisions", "issues", "experiments"];
  const counts: Record<string, number> = {};
  for (const table of tables) {
    const stmt = conn.prepare(`SELECT COUNT(*) AS n FROM ${table}`);
    const result = stmt.get() as { n: number };
    counts[table] = result.n;
  }
  return counts;
}

function migrateV2Project(conn: DatabaseSync, dbPath: string): void {
  let uv = readUserVersion(conn);
  const tables = tableNames(conn);

  if (uv === 0) {
    if (tables.has("meta")) {
      const stmt = conn.prepare("SELECT value FROM meta WHERE key='schema_version'");
      const row = stmt.get() as { value: string } | undefined;
      if (row && row.value) {
        const parsed = parseInt(row.value, 10);
        if (isNaN(parsed)) {
          throw new MigrationError(`无效 schema_version: ${row.value}`);
        }
        uv = parsed;
        try {
          conn.exec("BEGIN IMMEDIATE");
          setUserVersion(conn, uv);
          conn.exec("COMMIT");
        } catch (err) {
          conn.exec("ROLLBACK");
          throw new MigrationError(`schema 版本收敛失败: ${err}`);
        }
      } else if (tables.size === 1 && tables.has("meta")) {
        try {
          conn.exec("BEGIN IMMEDIATE");
          createProjectV2(conn);
          setUserVersion(conn, SCHEMA_VERSION);
          conn.exec("COMMIT");
          return;
        } catch (err) {
          conn.exec("ROLLBACK");
          throw new MigrationError(`schema 迁移失败: ${err}`);
        }
      } else {
        throw new MigrationError("无法确定项目库 schema 版本");
      }
    } else if (tables.size === 0) {
      try {
        conn.exec("BEGIN IMMEDIATE");
        createProjectV2(conn);
        setUserVersion(conn, SCHEMA_VERSION);
        conn.exec("COMMIT");
        return;
      } catch (err) {
        conn.exec("ROLLBACK");
        throw new MigrationError(`schema 迁移失败: ${err}`);
      }
    }
  }

  if (uv >= SCHEMA_VERSION) {
    return;
  }

  if (uv !== 1) {
    throw new MigrationError(`未知 schema 版本: ${uv}`);
  }

  const backupPath = `${dbPath}.pre-v2.bak`;
  if (!fs.existsSync(backupPath)) {
    try {
      fs.copyFileSync(dbPath, backupPath);
    } catch (err) {
      throw new MigrationError(`创建迁移备份失败: ${err}`);
    }
  }

  try {
    conn.exec("BEGIN IMMEDIATE");
    const before = projectRowCounts(conn);
    step1To1_1(conn);
    step1_1To2(conn);
    const after = projectRowCounts(conn);

    for (const table of Object.keys(before)) {
      if (before[table] !== after[table]) {
        throw new MigrationError("迁移前后核心表行数不一致");
      }
    }

    setUserVersion(conn, SCHEMA_VERSION);
    conn.exec("COMMIT");
  } catch (err) {
    conn.exec("ROLLBACK");
    if (err instanceof MigrationError) {
      throw err;
    }
    throw new MigrationError(`schema 迁移失败: ${err}`);
  }
}

export function initProjectDb(projectDir: string, slug: string): string {
  const dbPath = projectDbPath(projectDir);
  const dir = path.dirname(dbPath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }

  let conn: DatabaseSync;
  if (fs.existsSync(dbPath)) {
    conn = new DatabaseSync(dbPath, { open: true });
  } else {
    conn = new DatabaseSync(dbPath, { open: true });
  }

  try {
    conn.exec("PRAGMA journal_mode=WAL");
    conn.exec("PRAGMA busy_timeout=30000");
    migrateV2Project(conn, dbPath);

    const stmt1 = conn.prepare("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)");
    stmt1.run("schema_version", "1");
    const stmt2 = conn.prepare("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)");
    stmt2.run("slug", slug);
  } finally {
    conn.close();
  }

  return dbPath;
}

function stepCentral1To2(conn: DatabaseSync): void {
  conn.exec("DELETE FROM journals WHERE id NOT IN (SELECT MIN(id) FROM journals GROUP BY name)");
  conn.exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_journals_name ON journals(name)");
  addColumnIfMissing(conn, "project_registry", "category", "category TEXT");
  addColumnIfMissing(conn, "project_registry", "archived_at", "archived_at TEXT");
}

function migrateV2Central(conn: DatabaseSync): void {
  const uv = readUserVersion(conn);
  if (uv >= SCHEMA_VERSION) {
    return;
  }

  try {
    conn.exec("BEGIN IMMEDIATE");
    const tables = tableNames(conn);

    if (tables.has("project_registry")) {
      const stmt = conn.prepare("PRAGMA table_info(project_registry)");
      const columns = stmt.all() as Array<{ name: string }>;
      const pcols = new Set(columns.map((c) => c.name));
      if (!pcols.has("db_path")) {
        throw new MigrationError("中央库 project_registry 列结构异常（缺 db_path 列）—— 无法安全迁移，请人工检查");
      }
      stepCentral1To2(conn);
    } else {
      for (const ddl of Object.values(CENTRAL_TABLES)) {
        conn.exec(ddl);
      }
      conn.exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_journals_name ON journals(name)");
    }

    setUserVersion(conn, SCHEMA_VERSION);
    conn.exec("COMMIT");
  } catch (err) {
    conn.exec("ROLLBACK");
    throw new MigrationError(`中央索引 schema 迁移失败: ${err}`);
  }
}

export function initCentralDb(): string {
  const dbPath = centralDbPath();
  const dir = path.dirname(dbPath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }

  let conn: DatabaseSync;
  if (fs.existsSync(dbPath)) {
    conn = new DatabaseSync(dbPath, { open: true });
  } else {
    conn = new DatabaseSync(dbPath, { open: true });
  }

  try {
    conn.exec("PRAGMA journal_mode=WAL");
    conn.exec("PRAGMA busy_timeout=30000");
    migrateV2Central(conn);
  } finally {
    conn.close();
  }

  return dbPath;
}

export function ensureProject(projectDir: string, slug: string, name: string = "", category: string = ""): string {
  const dbPath = initProjectDb(projectDir, slug);
  initCentralDb();
  const idxPath = centralDbPath();

  type RegistryRow = { db_path: string; name: string; category: string };
  const existing = rows<RegistryRow>(idxPath, "SELECT db_path, name, category FROM project_registry WHERE slug=?", [slug]);

  if (existing.length > 0 && existing[0].db_path === dbPath && existing[0].category === category) {
    return dbPath;
  }

  exec(
    idxPath,
    `INSERT INTO project_registry(slug, db_path, name, category, updated_at)
     VALUES(?,?,?,?,?)
     ON CONFLICT(slug) DO UPDATE SET db_path=excluded.db_path,
                                      name=excluded.name,
                                      category=COALESCE(excluded.category, project_registry.category),
                                      updated_at=excluded.updated_at`,
    [slug, dbPath, name || slug, category || null, nowIso()]
  );

  return dbPath;
}

// ---------------------------------------------------------------------------
// Project resolution
// ---------------------------------------------------------------------------

export function resolveProjectDb(project: string, cwd: string = ""): string | ArchivedProjectPath | null {
  const cand = project.trim();

  function withArchiveStatus(p: string): string | ArchivedProjectPath {
    const idxPath = centralDbPath();
    if (fs.existsSync(idxPath)) {
      type StatusRow = { status: string };
      const statusRows = rows<StatusRow>(idxPath, "SELECT status FROM project_registry WHERE db_path=?", [p]);
      if (statusRows.length > 0 && statusRows[0].status === "archived") {
        return new ArchivedProjectPath(p);
      }
    }
    return p;
  }

  if (!cand) {
    if (cwd) {
      const cwdDb = path.join(cwd, "fact.db");
      if (fs.existsSync(cwdDb)) {
        return withArchiveStatus(cwdDb);
      }
    }
    return null;
  }

  const candidate = path.resolve(cand);
  const candidateDb = path.join(candidate, "fact.db");
  if (fs.existsSync(candidateDb)) {
    return withArchiveStatus(candidateDb);
  }

  const idx = centralDbPath();
  if (fs.existsSync(idx)) {
    type RegistryRow = { db_path: string; status: string };
    const regRows = rows<RegistryRow>(idx, "SELECT db_path, status FROM project_registry WHERE slug=?", [cand]);
    if (regRows.length > 0 && fs.existsSync(regRows[0].db_path)) {
      const p = regRows[0].db_path;
      if (regRows[0].status === "archived") {
        return new ArchivedProjectPath(p);
      }
      return p;
    }
  }

  return null;
}

// ---------------------------------------------------------------------------
// Generic helpers
// ---------------------------------------------------------------------------

export function statusOf(dbPath: string): Record<string, any> {
  const out: Record<string, any> = {};
  const allowedTables = ["tasks", "decisions", "issues", "experiments", "goals", "events"];

  for (const table of allowedTables) {
    try {
      const result = rows<{ n: number }>(dbPath, `SELECT COUNT(*) AS n FROM ${table}`);
      out[table] = result[0].n;
    } catch {
      out[table] = 0;
    }
  }

  out.recent_tasks = recent(dbPath, "tasks");
  out.recent_decisions = recent(dbPath, "decisions");
  out.recent_issues = recent(dbPath, "issues");
  out.recent_experiments = recent(dbPath, "experiments");

  return out;
}

function recent(dbPath: string, table: string, limit: number = 5): any[] {
  const allowedTables = ["tasks", "decisions", "issues", "experiments", "goals", "events"];
  if (!allowedTables.includes(table)) {
    return [];
  }
  const orderCol = "updated_at";
  try {
    return rows(dbPath, `SELECT * FROM ${table} ORDER BY ${orderCol} DESC LIMIT ?`, [limit]);
  } catch {
    return [];
  }
}

export function touchActivity(dbPath: string): void {
  const idx = centralDbPath();
  if (!fs.existsSync(idx)) {
    return;
  }

  type SlugRow = { slug: string };
  const slugRows = rows<SlugRow>(idx, "SELECT slug FROM project_registry WHERE db_path=?", [dbPath]);
  if (slugRows.length > 0) {
    exec(idx, "UPDATE project_registry SET last_activity_at=? WHERE slug=?", [nowIso(), slugRows[0].slug]);
  }
}

export function validateProjectDir(projectDir: string): [string | null, string | null] {
  const p = path.resolve(projectDir);
  if (!fs.existsSync(p)) {
    return [null, `目录不存在: ${projectDir}`];
  }
  const stats = fs.statSync(p);
  if (!stats.isDirectory()) {
    return [null, `不是目录: ${projectDir}`];
  }
  return [path.join(p, "fact.db"), null];
}

// ---------------------------------------------------------------------------
// journal helpers (central)
// ---------------------------------------------------------------------------

export const JOURNAL_COLS = [
  "name", "publisher", "abbrev", "issn", "if_year", "if_value", "jcr_quartile",
  "abstract_format", "abstract_max_words", "abstract_required_headers",
  "imrd_required", "prisma_abstract_recommended", "reporting_checklist_required",
  "detailed_search_strategies_required", "word_limit_main", "word_over_limit_fee",
  "registration_recommended", "narrative_review_policy", "manuscript_structure",
  "title_format_recommendation", "keywords_count_min", "keywords_count_max",
  "mesh_keywords_recommended", "submission_url", "source_url", "notes",
];

export function upsertJournal(data: Record<string, any>): string {
  const dbPath = initCentralDb();
  const fields = JOURNAL_COLS.filter((c) => c in data);

  if (fields.length === 0) {
    throw new Error("没有可写入的 journals 字段");
  }

  const name = data.name;
  if (!name) {
    throw new Error("journals 必须有 name");
  }

  const placeholders = fields.map(() => "?").join(", ");
  const values = fields.map((c) => data[c]);

  exec(dbPath, `INSERT OR IGNORE INTO journals(${fields.join(", ")}) VALUES(${placeholders})`, values);

  const updCols = fields.filter((c) => c !== "name");
  if (updCols.length > 0) {
    const updSet = updCols.map((c) => `${c}=?`).join(", ");
    const updValues = updCols.map((c) => data[c]);
    exec(dbPath, `UPDATE journals SET ${updSet}, updated_at=? WHERE name=?`, [...updValues, nowIso(), name]);
  }

  type IdRow = { id: number };
  const idRows = rows<IdRow>(dbPath, "SELECT id FROM journals WHERE name=?", [name]);
  return idRows.length > 0 ? String(idRows[0].id) : "?";
}

export function queryJournals(filters: Record<string, any>, limit: number = 20): any[] {
  const idx = centralDbPath();
  if (!fs.existsSync(idx)) {
    return [];
  }

  const where: string[] = [];
  const params: any[] = [];

  for (const key of ["name", "abbrev", "publisher", "issn", "jcr_quartile"]) {
    const val = filters[key];
    if (val) {
      where.push(`${key} LIKE ?`);
      params.push(`%${val}%`);
    }
  }

  let sql = "SELECT * FROM journals";
  if (where.length > 0) {
    sql += " WHERE " + where.join(" AND ");
  }
  sql += " ORDER BY name LIMIT ?";
  params.push(limit);

  return rows(idx, sql, params);
}
