/**
 * fact-infra plugin handlers — 科研项目管理的事实真相源（Fact Layer).
 *
 * 19 个 handler 函数，与 Python 版本 1:1 对应
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as store from "./store.js";

// ---------------------------------------------------------------------------
// JSON helpers
// ---------------------------------------------------------------------------

function ok(payload: Record<string, any>): string {
  return JSON.stringify({ ok: true, ...payload }, null, 1);
}

function err(message: string, extra: Record<string, any> = {}): string {
  return JSON.stringify({ error: message, ...extra }, null, 1);
}

function need(args: Record<string, any>, key: string): [any, string | null] {
  const val = args[key];
  if (val === null || val === undefined || (typeof val === "string" && !val.trim())) {
    return [null, `缺少必填参数: ${key}`];
  }
  return [val, null];
}

function resolve(args: Record<string, any>): [string | null, string | null] {
  const project = (args.project || "").trim();
  const cwd = ""; // OpenClaw 无 cwd 回退，只保留 slug 或绝对路径
  const dbPath = store.resolveProjectDb(project, cwd);

  if (dbPath === null) {
    const candidate = path.resolve(project);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return [null, `目录存在但无 fact.db，先 fact_init: ${candidate}`];
    }
    return [null, `无法定位项目 fact.db（project=${project}）。先用 fact_init 初始化，或传项目目录/slug。`];
  }

  if (dbPath instanceof store.ArchivedProjectPath) {
    return [null, `项目已归档，路径 ${String(dbPath)}（用 fact_rebind activate=true 恢复）`];
  }

  return [dbPath, null];
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

export function hFactInit(args: Record<string, any>): string {
  const [projectDir, err1] = need(args, "project_dir");
  if (err1) return err(err1);

  const [slug, err2] = need(args, "slug");
  if (err2) return err(err2);

  const name = (args.name || slug).trim();
  const projectType = (args.project_type || "").trim();

  if (!(projectType in store.PHASE_TEMPLATES)) {
    return err("project_type 必须是有效类型: " + Object.keys(store.PHASE_TEMPLATES).join(", "));
  }

  const [dbPath, err3] = store.validateProjectDir(projectDir);
  if (err3) return err(err3);

  try {
    const idx = store.initCentralDb();
    type RegRow = { status: string; category: string };
    const existing = store.rows<RegRow>(idx, "SELECT status, category FROM project_registry WHERE slug=?", [slug]);

    if (existing.length > 0 && existing[0].status === "archived") {
      return err("项目已归档，请用 fact_rebind activate=true 恢复后再初始化");
    }

    const db = store.ensureProject(projectDir, slug, name, projectType);

    type MetaRow = { key: string; value: string };
    const metaRows = store.rows<MetaRow>(db, "SELECT key, value FROM meta");
    const old: Record<string, string> = {};
    for (const r of metaRows) {
      old[r.key] = r.value;
    }

    const oldType = old.phase_template;
    const template = store.PHASE_TEMPLATES[projectType];
    const encoded = JSON.stringify(template);

    let oldParsed: string[] | null = null;
    if (oldType) {
      try {
        oldParsed = JSON.parse(oldType);
      } catch {
        oldParsed = null;
      }
    }

    if (JSON.stringify(oldParsed) !== JSON.stringify(template)) {
      store.exec(db, "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", ["phase_template", encoded]);
      store.exec(db, "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", ["current_phase", template[0]]);
      store.exec(db, "INSERT INTO events(text, created_at) VALUES(?,?)", [`初始化项目类型: ${projectType}`, store.nowIso()]);
    }
  } catch (exc: any) {
    return err(`初始化失败: ${exc}`);
  }

  return ok({ message: "项目 fact.db 已初始化", db_path: dbPath, slug });
}

export function hFactStatus(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  try {
    const status = store.statusOf(dbPath!);

    type MetaRow = { key: string; value: string };
    const metaRows = store.rows<MetaRow>(dbPath!, "SELECT key, value FROM meta");
    const meta: Record<string, string> = {};
    for (const r of metaRows) {
      meta[r.key] = r.value;
    }

    const currentPhase = meta.current_phase;
    let phaseTemplate: string[] | null = null;
    if (meta.phase_template) {
      try {
        phaseTemplate = JSON.parse(meta.phase_template);
      } catch {
        phaseTemplate = null;
      }
    }

    const recentEvents = store.rows(dbPath!, "SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT 3");

    type GoalRow = { id: number; title: string; phase: string; done_criteria: string; position: number };
    const activeGoals = store.rows<GoalRow>(
      dbPath!,
      "SELECT id, title, phase, done_criteria, position FROM goals WHERE status='active' ORDER BY COALESCE(position, 999), id"
    );

    type DoneRow = { phase: string; n: number };
    const doneByPhase = store.rows<DoneRow>(dbPath!, "SELECT phase, COUNT(*) AS n FROM goals WHERE status='done' GROUP BY phase");

    let warning: string | null = null;
    if (currentPhase && activeGoals.length > 0) {
      const activePhases = new Set(activeGoals.filter((g) => g.phase).map((g) => g.phase));
      if (phaseTemplate && activePhases.size > 0) {
        try {
          const curIdx = phaseTemplate.indexOf(currentPhase);
          if (curIdx >= 0) {
            const stuck = Array.from(activePhases).filter((p) => {
              const pIdx = phaseTemplate!.indexOf(p);
              return pIdx >= 0 && pIdx < curIdx;
            });
            if (stuck.length > 0) {
              warning = `存在更早 phase 的 active goals（未推进）: ${stuck.sort().join(", ")}`;
            }
          }
        } catch {
          // ignore
        }
      }
    }

    const location = {
      current_phase: currentPhase,
      phase_template: phaseTemplate,
      recent_events: recentEvents,
    };

    const goalsSection = {
      active_goals: activeGoals,
      done_by_phase: doneByPhase,
    };

    const staleDays = parseInt(meta.stale_days || "14", 10);
    type ActRow = { last_activity_at: string };
    const idxRows = store.rows<ActRow>(store.centralDbPath(), "SELECT last_activity_at FROM project_registry WHERE db_path=?", [dbPath!]);

    let stale = false;
    if (idxRows.length > 0 && idxRows[0].last_activity_at) {
      try {
        const ts = store.parseTimestampUtc(idxRows[0].last_activity_at);
        if (!ts) throw new Error("timestamp parse failed");

        const age = (Date.now() - ts.getTime()) / 86400000;
        let newest = fs.statSync(dbPath!).mtimeMs / 1000;
        const root = path.dirname(dbPath!);

        const walk = (dir: string, depth: number) => {
          if (depth > 2) return;
          const entries = fs.readdirSync(dir, { withFileTypes: true });
          for (const ent of entries) {
            const fullPath = path.join(dir, ent.name);
            if (ent.isDirectory()) {
              walk(fullPath, depth + 1);
            } else if (ent.isFile() && !ent.name.startsWith("fact.db")) {
              const mtime = fs.statSync(fullPath).mtimeMs / 1000;
              if (mtime > newest) newest = mtime;
            }
          }
        };

        walk(root, 0);
        stale = age > staleDays && fs.statSync(dbPath!).mtimeMs / 1000 < newest;
      } catch {
        // ignore
      }
    }

    return ok({
      db_path: dbPath,
      summary: status,
      location,
      goals: goalsSection,
      warning,
      stale_warning: stale ? "项目可能已过期，请更新活动记录" : null,
    });
  } catch (exc: any) {
    return err(`读取状态失败: ${exc}`);
  }
}

export function hFactQuery(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const table = (args.table || "").trim();
  if (table) {
    const allowed = ["tasks", "decisions", "issues", "experiments", "meta", "goals", "events"];
    if (!allowed.includes(table)) {
      return err(`未知表: ${table}（可用 ${allowed.join("/")}）`);
    }

    let limit = parseInt(args.limit || "50", 10);
    limit = Math.max(1, Math.min(limit, 200));

    try {
      const result = store.rows(dbPath!, `SELECT * FROM ${table} LIMIT ?`, [limit]);
      return ok({ db_path: dbPath, table, count: result.length, rows: result });
    } catch (exc: any) {
      return err(`查询失败: ${exc}`);
    }
  }

  const goalId = args.goal_id;
  if (goalId !== null && goalId !== undefined) {
    try {
      const goal = store.rows(dbPath!, "SELECT * FROM goals WHERE id=?", [goalId]);
      if (goal.length === 0) {
        return err(`goal_id 不存在: ${goalId}`);
      }

      const tasks = store.rows(dbPath!, "SELECT * FROM tasks WHERE goal_id=? ORDER BY id", [goalId]);
      const issues = store.rows(dbPath!, "SELECT * FROM issues WHERE goal_id=? ORDER BY id", [goalId]);
      const experiments = store.rows(dbPath!, "SELECT * FROM experiments WHERE goal_id=? ORDER BY id", [goalId]);
      const subgoals = store.rows(dbPath!, "SELECT * FROM goals WHERE parent_goal_id=? ORDER BY id", [goalId]);
      const events = store.rows(dbPath!, "SELECT * FROM events WHERE goal_id=? ORDER BY id", [goalId]);

      return ok({
        db_path: dbPath,
        goal: goal[0],
        tasks,
        issues,
        experiments,
        subgoals,
        events,
      });
    } catch (exc: any) {
      return err(`goal 明细查询失败: ${exc}`);
    }
  }

  return err("缺少参数: table 或 goal_id");
}

export function hFactIndex(args: Record<string, any>): string {
  const idx = store.centralDbPath();

  try {
    store.initCentralDb();

    const clauses: string[] = [];
    const params: any[] = [];

    if (args.category) {
      clauses.push("category=?");
      params.push(args.category);
    }
    if (args.status) {
      clauses.push("status=?");
      params.push(args.status);
    }

    const whereSql = clauses.length > 0 ? " WHERE " + clauses.join(" AND ") : "";
    const projects = store.rows(idx, `SELECT * FROM project_registry${whereSql} ORDER BY slug`, params);

    for (const row of projects) {
      row.path_ok = fs.existsSync(row.db_path || "");
      if (!row.path_ok) {
        row["提示"] = "路径失联，请用 fact_rebind 修复";
      }

      try {
        const ts = store.parseTimestampUtc(row.last_activity_at || "");
        let age: number;
        if (ts) {
          age = (Date.now() - ts.getTime()) / 86400000;
        } else {
          age = (Date.now() - fs.statSync(row.db_path).mtimeMs) / 86400000;
        }
        if (age > 14) row.stale = true;
      } catch {
        // ignore
      }
    }

    const journals = store.rows(idx, "SELECT id, name, abbrev, if_value, jcr_quartile FROM journals ORDER BY name");

    return ok({ projects, journals, index_db: idx });
  } catch (exc: any) {
    return err(`读取中央索引失败: ${exc}`);
  }
}

export function hFactRebind(args: Record<string, any>): string {
  const [project, needErr] = need(args, "project");
  if (needErr) return err(needErr);

  const idx = store.initCentralDb();

  try {
    type RegRow = { db_path: string; status: string };
    const rows = store.rows<RegRow>(idx, "SELECT * FROM project_registry WHERE slug=?", [project.trim()]);
    if (rows.length === 0) {
      return err(`未找到项目注册记录: ${project}`);
    }

    const row = rows[0];
    if (row.status === "archived" && !args.activate) {
      return err("项目已归档，确认后请传 activate=true");
    }

    const before = row.db_path;
    const requested = (args.db_path || "").trim();
    let candidates: string[] = [];

    if (requested) {
      candidates = [requested];
    } else {
      const projectsRoot = process.env.FACT_PROJECTS_ROOT || "/home/node/.openclaw/workspace/projects";
      const root = path.join(projectsRoot, project.trim());
      if (fs.existsSync(root) && fs.statSync(root).isDirectory()) {
        candidates.push(path.join(root, "fact.db"));
        const entries = fs.readdirSync(root, { withFileTypes: true });
        for (const ent of entries) {
          if (ent.isDirectory()) {
            candidates.push(path.join(root, ent.name, "fact.db"));
          }
        }
      }
    }

    const valid = candidates.filter((p) => {
      try {
        return fs.existsSync(p) && fs.statSync(p).isFile() && fs.statSync(p).size > 0;
      } catch {
        return false;
      }
    });

    if (valid.length > 1) {
      return err(`探测到 ${valid.length} 个候选 fact.db，请显式提供 db_path 参数，候选: ${valid.join(" | ")}`);
    }
    if (valid.length === 0) {
      return err("未探测到可用 fact.db，请显式提供 db_path（不猜路径）");
    }

    const after = valid[0];
    let activated = false;

    if (row.status === "archived" && args.activate) {
      store.exec(idx, "UPDATE project_registry SET status='active', archived_at=NULL, db_path=?, updated_at=? WHERE slug=?", [
        after,
        store.nowIso(),
        project.trim(),
      ]);
      activated = true;
    } else {
      store.exec(idx, "UPDATE project_registry SET db_path=?, updated_at=? WHERE slug=?", [after, store.nowIso(), project.trim()]);
    }

    let msg = "registry 路径已修复";
    if (activated) {
      msg += "，项目已重新激活";
    }

    return ok({ message: msg, project: project.trim(), before, after });
  } catch (exc: any) {
    return err(`fact_rebind 失败: ${exc}`);
  }
}

export function hFactArchive(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const idx = store.centralDbPath();

  try {
    store.initCentralDb();

    type SlugRow = { slug: string };
    const rows = store.rows<SlugRow>(idx, "SELECT slug FROM project_registry WHERE db_path=?", [dbPath!]);
    if (rows.length === 0) {
      return err(`项目未注册，无法归档: ${dbPath}`);
    }

    type GoalRow = { id: number; title: string };
    const goals = store.rows<GoalRow>(dbPath!, "SELECT id, title FROM goals WHERE status='active'");
    type IssueRow = { id: number; title: string };
    const issues = store.rows<IssueRow>(dbPath!, "SELECT id, title FROM issues WHERE status='open'");

    if ((goals.length > 0 || issues.length > 0) && !args.force) {
      return err("存在未关闭项目项", { active_goals: goals, open_issues: issues });
    }

    const now = store.nowIso();
    const slug = rows[0].slug;
    store.exec(idx, "UPDATE project_registry SET status='archived', archived_at=?, updated_at=? WHERE slug=?", [now, now, slug]);

    return ok({
      message: `项目已标记 archived: ${slug}`,
      guidance: ["fact_archive 已完成", `请人工 mv 项目目录到 已完成归档/${slug}/，若 db_path 失效再运行 fact_rebind`],
    });
  } catch (exc: any) {
    return err(`归档失败: ${exc}`);
  }
}

export function hFactGoalAdd(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [title, err1] = need(args, "title");
  if (err1) return err(err1);

  const [doneCriteria, err2] = need(args, "done_criteria");
  if (err2) return err(err2);

  const parentGoalId = args.parent_goal_id;
  let position = args.position;
  const phase = (args.phase || "").trim() || null;

  try {
    if (parentGoalId !== null && parentGoalId !== undefined) {
      const parentRows = store.rows(dbPath!, "SELECT id FROM goals WHERE id=?", [parentGoalId]);
      if (parentRows.length === 0) {
        return err(`parent_goal_id 不存在: ${parentGoalId}`);
      }
    }

    if (position === null || position === undefined) {
      type PosRow = { next_pos: number };
      const posRows = store.rows<PosRow>(
        dbPath!,
        "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos FROM goals WHERE parent_goal_id IS ?",
        [parentGoalId || null]
      );
      position = posRows[0].next_pos;
    }

    if (phase) {
      type TemplRow = { value: string };
      const templates = store.rows<TemplRow>(dbPath!, "SELECT value FROM meta WHERE key='phase_template'");
      if (templates.length > 0 && templates[0].value) {
        try {
          const tmpl = JSON.parse(templates[0].value);
          if (!tmpl.includes(phase)) {
            // 软约束：警告放行
          }
        } catch {
          // ignore
        }
      }
    }

    store.exec(
      dbPath!,
      "INSERT INTO goals(title, done_criteria, status, parent_goal_id, phase, position, origin) VALUES(?,?,?,?,?,?,'normal')",
      [title, doneCriteria, "active", parentGoalId || null, phase, position]
    );

    type IdRow = { id: number };
    const goalIdRows = store.rows<IdRow>(dbPath!, "SELECT last_insert_rowid() AS id");
    const goalId = goalIdRows[0].id;

    store.exec(dbPath!, "INSERT INTO events(goal_id, phase, text) VALUES(?,?,?)", [goalId, phase, `goal 创建: ${title}`]);
    store.touchActivity(dbPath!);

    return ok({ goal_id: goalId, title, done_criteria: doneCriteria, phase, position, message: "goal 已创建" });
  } catch (exc: any) {
    return err(`goal 创建失败: ${exc}`);
  }
}

export function hFactGoalUpdate(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const goalId = args.goal_id;
  if (goalId === null || goalId === undefined) {
    return err("缺少必填参数: goal_id");
  }

  const status = (args.status || "").trim();
  const title = (args.title || "").trim();
  const doneCriteria = (args.done_criteria || "").trim();
  const phase = (args.phase || "").trim();
  const confirm = !!args.confirm;

  try {
    type GoalRow = { id: number; done_criteria: string; origin: string };
    const goalRows = store.rows<GoalRow>(dbPath!, "SELECT * FROM goals WHERE id=?", [goalId]);
    if (goalRows.length === 0) {
      return err(`goal_id 不存在: ${goalId}`);
    }

    const g = goalRows[0];
    let warning: string | null = null;

    if ((status === "done" || status === "abandoned") && !confirm) {
      const openItems: string[] = [];

      for (const tbl of ["tasks", "issues"]) {
        type ItemRow = { id: number; title: string; status: string };
        const items = store.rows<ItemRow>(dbPath!, `SELECT id, title, status FROM ${tbl} WHERE goal_id=?`, [goalId]);
        for (const r of items) {
          if (["open", "todo", "doing", "blocked"].includes(r.status)) {
            openItems.push(`${tbl}#${r.id} ${r.title} (${r.status})`);
          }
        }
      }

      type ExpRow = { id: number; name: string; verdict: string };
      const exp = store.rows<ExpRow>(dbPath!, "SELECT id, name, verdict FROM experiments WHERE goal_id=?", [goalId]);
      for (const r of exp) {
        if (r.verdict === "pending") {
          openItems.push(`experiments#${r.id} ${r.name} (pending)`);
        }
      }

      if (openItems.length > 0) {
        return err(`goal 有未收尾项，需 confirm=true 关闭。未收尾: ${openItems.slice(0, 10).join(" | ")}`);
      }
    }

    const sets: string[] = [];
    const params: any[] = [];

    if (status) {
      if (!["active", "done", "abandoned"].includes(status)) {
        return err(`非法 status: ${status}`);
      }
      sets.push("status=?");
      params.push(status);

      if (status === "done" || status === "abandoned") {
        sets.push("completed_at=?");
        params.push(store.nowIso());
      }

      if (status === "done" && !(g.done_criteria || doneCriteria)) {
        if (!g.origin || g.origin === "normal") {
          return err("done_criteria 必填（K7）：goal 无完成判据不能标记 done");
        }
        warning = "done_criteria 为空（回填豁免 origin=backfilled），已放行";
      }
    }

    if (title) {
      sets.push("title=?");
      params.push(title);
    }

    if (doneCriteria) {
      sets.push("done_criteria=?");
      params.push(doneCriteria);
    }

    if (phase) {
      sets.push("phase=?");
      params.push(phase);
    }

    if (sets.length > 0) {
      params.push(goalId);
      store.exec(dbPath!, `UPDATE goals SET ${sets.join(", ")} WHERE id=?`, params);
      store.exec(dbPath!, "INSERT INTO events(goal_id, text) VALUES(?,?)", [goalId, `goal status 变更: ${status || "update"}`]);
      store.touchActivity(dbPath!);
    }

    return ok({ goal_id: goalId, message: "goal 已更新", warning });
  } catch (exc: any) {
    return err(`goal 更新失败: ${exc}`);
  }
}

export function hFactPhaseSet(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [phase, needErr] = need(args, "phase");
  if (needErr) return err(needErr);

  const confirm = !!args.confirm;

  try {
    type GoalRow = { id: number; title: string; phase: string };
    const active = store.rows<GoalRow>(dbPath!, "SELECT id, title, phase FROM goals WHERE status='active' ORDER BY id");

    if (active.length > 0 && !confirm) {
      const list = active.slice(0, 10).map((g) => `#${g.id} ${g.title}`).join(" | ");
      return err(`存在 ${active.length} 个 active goals，需 confirm=true 才可改 phase。active: ${list}`);
    }

    type MetaRow = { value: string };
    const oldRows = store.rows<MetaRow>(dbPath!, "SELECT value FROM meta WHERE key='current_phase'");
    const oldPhase = oldRows.length > 0 ? oldRows[0].value : null;

    let warning: string | null = null;
    const templates = store.rows<MetaRow>(dbPath!, "SELECT value FROM meta WHERE key='phase_template'");
    if (templates.length > 0 && templates[0].value) {
      try {
        const tmpl = JSON.parse(templates[0].value);
        if (!tmpl.includes(phase)) {
          warning = `phase 不在模板中: ${phase}（模板: ${tmpl.join("/")}）`;
        }
      } catch {
        // ignore
      }
    }

    store.exec(dbPath!, "INSERT OR REPLACE INTO meta(key, value) VALUES('current_phase', ?)", [phase]);
    store.exec(dbPath!, "INSERT INTO events(phase, text) VALUES(?,?)", [oldPhase, `phase: ${oldPhase} → ${phase}`]);
    store.touchActivity(dbPath!);

    return ok({ message: "current_phase 已更新", phase, old_phase: oldPhase, warning });
  } catch (exc: any) {
    return err(`phase 设置失败: ${exc}`);
  }
}

export function hFactNoteAdd(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [text, needErr] = need(args, "text");
  if (needErr) return err(needErr);

  const goalId = args.goal_id;
  let refs = args.refs;
  let refsText: string | null = null;

  if (refs !== null && refs !== undefined) {
    try {
      const parsed = typeof refs === "string" ? JSON.parse(refs) : refs;
      if (!Array.isArray(parsed)) {
        return err("refs 必须是 JSON 数组");
      }
      for (const item of parsed) {
        if (typeof item !== "object" || !["file", "decision_key", "issue_key", "task_id"].includes(item.type)) {
          return err(`refs 项越界: ${JSON.stringify(item)}（type 须 file/decision_key/issue_key/task_id）`);
        }
      }
      refsText = JSON.stringify(parsed);
    } catch (exc: any) {
      return err(`refs 非法 JSON: ${exc}`);
    }
  }

  try {
    type PhaseRow = { value: string };
    const phaseRows = store.rows<PhaseRow>(dbPath!, "SELECT value FROM meta WHERE key='current_phase'");
    const phase = phaseRows.length > 0 ? phaseRows[0].value : null;

    store.exec(dbPath!, "INSERT INTO events(goal_id, phase, text, refs) VALUES(?,?,?,?)", [goalId || null, phase, text, refsText]);
    store.touchActivity(dbPath!);

    return ok({ message: "note 已记录", text, phase });
  } catch (exc: any) {
    return err(`note 写入失败: ${exc}`);
  }
}

export function hFactTaskAdd(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [title, needErr] = need(args, "title");
  if (needErr) return err(needErr);

  const ttype = (args.type || "task").trim();
  const priority = (args.priority || "medium").trim();
  const notes = (args.notes || "").trim();

  try {
    store.exec(dbPath!, "INSERT INTO tasks(title, type, status, priority, notes) VALUES(?,?,?,?,?)", [title, ttype, "todo", priority, notes]);
    store.touchActivity(dbPath!);

    return ok({ message: "task 已记录", title });
  } catch (exc: any) {
    return err(`写入失败: ${exc}`);
  }
}

export function hFactTaskUpdate(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const taskId = args.task_id;
  const status = (args.status || "").trim();
  const title = (args.title || "").trim();

  if (taskId === null || taskId === undefined) {
    return err("缺少必填参数: task_id");
  }

  const sets: string[] = [];
  const params: any[] = [];

  if (status) {
    if (!["todo", "doing", "done", "blocked", "cancelled"].includes(status)) {
      return err(`非法 status: ${status}`);
    }
    sets.push("status=?");
    params.push(status);

    if (status === "done") {
      sets.push("completed_at=?");
      params.push(store.nowIso());
    }
  }

  if (title) {
    sets.push("title=?");
    params.push(title);
  }

  if (sets.length === 0) {
    return err("没有可更新的字段（status/title 至少要给一个）");
  }

  sets.push("updated_at=?");
  params.push(store.nowIso());
  params.push(taskId);

  try {
    const rowcount = store.execChange(dbPath!, `UPDATE tasks SET ${sets.join(", ")} WHERE id=?`, params);
    if (rowcount === 0) {
      return err(`task_id 不存在: ${taskId}`);
    }
    store.touchActivity(dbPath!);

    return ok({ message: `task ${taskId} 已更新`, status: status || null });
  } catch (exc: any) {
    return err(`更新失败: ${exc}`);
  }
}

export function hFactDecisionAdd(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [title, needErr] = need(args, "title");
  if (needErr) return err(needErr);

  const decisionKey = (args.decision_key || "").trim();
  const summary = (args.summary || "").trim();
  const rationale = (args.rationale || "").trim();
  const sourceFile = (args.source_file || "").trim();

  try {
    store.exec(
      dbPath!,
      "INSERT INTO decisions(decision_key, title, status, summary, rationale, source_file) VALUES(?,?,?,?,?,?)",
      [decisionKey || null, title, "active", summary, rationale, sourceFile]
    );
    store.touchActivity(dbPath!);

    return ok({ message: "decision 已记录", title });
  } catch (exc: any) {
    return err(`写入失败: ${exc}`);
  }
}

export function hFactDecisionResolve(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [decisionKey, needErr] = need(args, "decision_key");
  if (needErr) return err(needErr);

  const status = (args.status || "resolved").trim();

  try {
    const rowcount = store.execChange(dbPath!, "UPDATE decisions SET status=?, updated_at=? WHERE decision_key=?", [
      status,
      store.nowIso(),
      decisionKey,
    ]);
    if (rowcount === 0) {
      return err(`decision_key 不存在: ${decisionKey}`);
    }
    store.touchActivity(dbPath!);

    return ok({ message: `decision ${decisionKey} 状态 → ${status}` });
  } catch (exc: any) {
    return err(`更新失败: ${exc}`);
  }
}

export function hFactIssueOpen(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [title, needErr] = need(args, "title");
  if (needErr) return err(needErr);

  const issueKey = (args.issue_key || "").trim();
  let severity = (args.severity || "medium").trim();
  const hypothesis = (args.hypothesis || "").trim();
  const workaround = (args.workaround || "").trim();

  if (!["high", "medium", "low"].includes(severity)) {
    severity = "medium";
  }

  try {
    store.exec(dbPath!, "INSERT INTO issues(issue_key, title, status, severity, hypothesis, workaround) VALUES(?,?,?,?,?,?)", [
      issueKey || null,
      title,
      "open",
      severity,
      hypothesis,
      workaround,
    ]);
    store.touchActivity(dbPath!);

    return ok({ message: "issue 已记录", title, issue_key: issueKey || null });
  } catch (exc: any) {
    return err(`写入失败: ${exc}`);
  }
}

export function hFactIssueResolve(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [issueKey, needErr] = need(args, "issue_key");
  if (needErr) return err(needErr);

  try {
    const rowcount = store.execChange(dbPath!, "UPDATE issues SET status='resolved', resolved_at=?, updated_at=? WHERE issue_key=?", [
      store.nowIso(),
      store.nowIso(),
      issueKey,
    ]);
    if (rowcount === 0) {
      return err(`issue_key 不存在: ${issueKey}`);
    }
    store.touchActivity(dbPath!);

    return ok({ message: `issue ${issueKey} 已解决` });
  } catch (exc: any) {
    return err(`更新失败: ${exc}`);
  }
}

export function hFactExpLog(args: Record<string, any>): string {
  const [dbPath, resErr] = resolve(args);
  if (resErr) return err(resErr);

  const [name, needErr] = need(args, "name");
  if (needErr) return err(needErr);

  const issueKey = (args.issue_key || "").trim();
  const inputConfig = (args.input_config || "").trim();
  const expectedResult = (args.expected_result || "").trim();
  const actualResult = (args.actual_result || "").trim();
  let verdict = (args.verdict || "pending").trim();
  const runAt = (args.run_at || "").trim();
  const notes = (args.notes || "").trim();

  if (!["conclude", "partial", "fail", "pending"].includes(verdict)) {
    verdict = "pending";
  }

  try {
    store.exec(
      dbPath!,
      "INSERT INTO experiments(issue_key, name, input_config, expected_result, actual_result, verdict, run_at, notes) VALUES(?,?,?,?,?,?,?,?)",
      [issueKey || null, name, inputConfig, expectedResult, actualResult, verdict, runAt || null, notes]
    );
    store.touchActivity(dbPath!);

    return ok({ message: "experiment 已记录", name, verdict });
  } catch (exc: any) {
    return err(`写入失败: ${exc}`);
  }
}

export function hFactJournalQuery(args: Record<string, any>): string {
  const filters: Record<string, string> = {};
  for (const k of ["name", "abbrev", "publisher", "issn", "jcr_quartile"]) {
    const val = (args[k] || "").trim();
    if (val) {
      filters[k] = val;
    }
  }

  let limit = parseInt(args.limit || "20", 10);
  limit = Math.max(1, Math.min(limit, 100));

  try {
    const journals = store.queryJournals(filters, limit);
    return ok({ count: journals.length, journals });
  } catch (exc: any) {
    return err(`查询失败: ${exc}`);
  }
}

export function hFactJournalUpsert(args: Record<string, any>): string {
  const [name, needErr] = need(args, "name");
  if (needErr) return err(needErr);

  const allowed = new Set(store.JOURNAL_COLS);
  const data: Record<string, any> = {};

  for (const [k, v] of Object.entries(args)) {
    if (allowed.has(k) && v !== null && v !== undefined) {
      data[k] = v;
    }
  }
  data.name = name;

  try {
    const jid = store.upsertJournal(data);
    return ok({ message: "journal 已写入/更新", journal_id: jid, name });
  } catch (exc: any) {
    return err(`写入失败: ${exc}`);
  }
}
