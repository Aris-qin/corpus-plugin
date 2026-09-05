/**
 * fact-infra plugin — OpenClaw 原生 TS tool plugin
 *
 * 移植自 hermes-fact-infra v0.3.0
 * 19 个 fact_* 工具，行为与 Python 版对齐
 */

import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import * as handlers from "./handlers.js";

export default defineToolPlugin({
  id: "fact-infra",
  name: "Fact Infrastructure",
  description: "科研项目管理的事实真相源（Fact Layer）。19 个 fact_* 工具：项目初始化、状态查询、任务/决策/问题/实验记录、目标管理、期刊画像。",
  tools: (tool) => [
    tool({
      name: "fact_init",
      label: "初始化项目 fact.db",
      description: "初始化一个项目的 fact.db 并注册到中央索引（唯一真源）。返回 JSON 字符串：{ok:true, db_path, slug} 或 {error:...}",
      parameters: Type.Object({
        project_dir: Type.String({ description: "项目目录绝对路径（如 /home/node/.openclaw/workspace/projects/lipo-degradation）" }),
        slug: Type.String({ description: "项目唯一标识（如 lipo-degradation）" }),
        name: Type.Optional(Type.String({ description: "项目显示名（可选，默认=slug）" })),
        project_type: Type.String({
          description: "项目类型（engineering/paper/review/revision/grant/patent/clinical/infra），决定 phase 模板",
          enum: ["engineering", "paper", "review", "revision", "grant", "patent", "clinical", "infra"],
        }),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactInit(params);
      },
    }),

    tool({
      name: "fact_status",
      label: "查看项目状态",
      description: "查看单项目事实总览（tasks/decisions/issues/experiments/goals/events 计数 + 最近动态 + 当前 phase + active goals）。返回 JSON 字符串：{ok:true, summary, location, goals} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug（中央索引注册的）或项目目录绝对路径" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactStatus(params);
      },
    }),

    tool({
      name: "fact_query",
      label: "查询项目数据",
      description: "通用查询项目 fact.db 的表内容 / goal 明细。传 table 参数查表全量（limit 行），传 goal_id 查 goal 关联的任务/子目标/事件。返回 JSON 字符串：{ok:true, rows/goal} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        table: Type.Optional(Type.String({ description: "tasks/decisions/issues/experiments/meta/goals/events", enum: ["tasks", "decisions", "issues", "experiments", "meta", "goals", "events"] })),
        goal_id: Type.Optional(Type.Integer({ description: "给定查 goal 明细（挂接任务/子目标/事件）" })),
        limit: Type.Optional(Type.Integer({ description: "最多返回行数(1-200)", minimum: 1, maximum: 200, default: 50 })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactQuery(params);
      },
    }),

    tool({
      name: "fact_index",
      label: "中央索引总览",
      description: "中央索引总览：所有注册项目 + journals 列表。可选 category/status 过滤。返回 JSON 字符串：{ok:true, projects[], journals[]} 或 {error:...}",
      parameters: Type.Object({
        category: Type.Optional(Type.String({ description: "过滤项目类型" })),
        status: Type.Optional(Type.String({ description: "过滤项目状态（active/archived）" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactIndex(params);
      },
    }),

    tool({
      name: "fact_rebind",
      label: "修复项目路径",
      description: "修复项目注册表中失联的 fact.db 路径。自动探测或显式指定 db_path。activate=true 可重新激活归档项目。返回 JSON 字符串：{ok:true, before, after} 或 {error:...}",
      parameters: Type.Object({
        project: Type.String({ description: "项目 slug" }),
        db_path: Type.Optional(Type.String({ description: "可选：显式指定新的 fact.db 路径" })),
        activate: Type.Optional(Type.Boolean({ description: "归档项目再激活", default: false })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactRebind(params);
      },
    }),

    tool({
      name: "fact_archive",
      label: "归档项目",
      description: "将项目标记为 archived（移出活跃视图）。存在 active goals / open issues 时需 force=true。返回 JSON 字符串：{ok:true, message, guidance[]} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        force: Type.Optional(Type.Boolean({ description: "强制归档（忽略未关闭项）", default: false })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactArchive(params);
      },
    }),

    tool({
      name: "fact_goal_add",
      label: "创建目标",
      description: "记录一条目标（goal），两层封顶 goal→subgoal。title + done_criteria 必填。返回 JSON 字符串：{ok:true, goal_id, title} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        title: Type.String({ description: "目标标题" }),
        done_criteria: Type.String({ description: "完成判据（必填；回填豁免走 origin=backfilled）" }),
        parent_goal_id: Type.Optional(Type.Integer({ description: "父 goal id（可选）" })),
        phase: Type.Optional(Type.String({ description: "归属 phase（可选，须在模板内，否则警告放行）" })),
        position: Type.Optional(Type.Integer({ description: "同 parent 下排序（缺省=max+1）" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactGoalAdd(params);
      },
    }),

    tool({
      name: "fact_goal_update",
      label: "更新目标",
      description: "更新目标状态/标题/判据（闭环检查：有未收尾项需 confirm）。status 可选 active/done/abandoned。返回 JSON 字符串：{ok:true, goal_id, message} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        goal_id: Type.Integer({ description: "目标 ID" }),
        status: Type.Optional(Type.String({ description: "active/done/abandoned", enum: ["active", "done", "abandoned"] })),
        title: Type.Optional(Type.String({ description: "新标题" })),
        done_criteria: Type.Optional(Type.String({ description: "新完成判据" })),
        phase: Type.Optional(Type.String({ description: "新 phase" })),
        confirm: Type.Optional(Type.Boolean({ description: "强制关闭未收尾项", default: false })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactGoalUpdate(params);
      },
    }),

    tool({
      name: "fact_phase_set",
      label: "设置项目阶段",
      description: "设置项目当前阶段（meta.current_phase 唯一真源）。存在 active goals 时需 confirm=true。返回 JSON 字符串：{ok:true, phase, old_phase} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        phase: Type.String({ description: "阶段名（如 验证/修回）" }),
        confirm: Type.Optional(Type.Boolean({ description: "存在 active goals 时需 confirm", default: false })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactPhaseSet(params);
      },
    }),

    tool({
      name: "fact_note_add",
      label: "记录进展笔记",
      description: "记录一条进展笔记（events 表，支持 refs 关联）。text 必填。返回 JSON 字符串：{ok:true, message, text, phase} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        text: Type.String({ description: "笔记内容" }),
        goal_id: Type.Optional(Type.Integer({ description: "关联的 goal id" })),
        refs: Type.Optional(Type.String({ description: "JSON 数组 [{\"type\":\"file|decision_key|issue_key|task_id\",\"value\":...}]" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactNoteAdd(params);
      },
    }),

    tool({
      name: "fact_task_add",
      label: "记录任务",
      description: "记录一条项目任务事实（todo）。title 必填。返回 JSON 字符串：{ok:true, message, title} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        title: Type.String({ description: "任务标题" }),
        type: Type.Optional(Type.String({ description: "类型：task/writing/experiment/review（默认 task）", default: "task" })),
        priority: Type.Optional(Type.String({ description: "high/medium/low（默认 medium）", enum: ["high", "medium", "low"], default: "medium" })),
        notes: Type.Optional(Type.String({ description: "备注" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactTaskAdd(params);
      },
    }),

    tool({
      name: "fact_task_update",
      label: "更新任务",
      description: "更新任务进度事实（status/title）。task_id 必填。返回 JSON 字符串：{ok:true, message} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        task_id: Type.Integer({ description: "任务 ID" }),
        status: Type.Optional(Type.String({ description: "todo/doing/done/blocked/cancelled", enum: ["todo", "doing", "done", "blocked", "cancelled"] })),
        title: Type.Optional(Type.String({ description: "新标题" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactTaskUpdate(params);
      },
    }),

    tool({
      name: "fact_decision_add",
      label: "记录决策",
      description: "记录一条决策+理由（科研关键事实）。title 必填。返回 JSON 字符串：{ok:true, message, title} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        title: Type.String({ description: "决策标题" }),
        decision_key: Type.Optional(Type.String({ description: "唯一键（如 AZTC-01）" })),
        summary: Type.Optional(Type.String({ description: "摘要" })),
        rationale: Type.Optional(Type.String({ description: "为什么这么定（核心）" })),
        source_file: Type.Optional(Type.String({ description: "来源文件" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactDecisionAdd(params);
      },
    }),

    tool({
      name: "fact_decision_resolve",
      label: "更新决策状态",
      description: "更新决策状态。decision_key 必填。返回 JSON 字符串：{ok:true, message} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        decision_key: Type.String({ description: "决策唯一键" }),
        status: Type.Optional(Type.String({ description: "active/superseded/resolved（默认 resolved）", default: "resolved" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactDecisionResolve(params);
      },
    }),

    tool({
      name: "fact_issue_open",
      label: "记录问题",
      description: "记录一个问题（审稿意见/隐患等）。title 必填。返回 JSON 字符串：{ok:true, message, title} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        title: Type.String({ description: "问题标题" }),
        issue_key: Type.Optional(Type.String({ description: "如 R4-2(1)" })),
        severity: Type.Optional(Type.String({ description: "high/medium/low", enum: ["high", "medium", "low"], default: "medium" })),
        hypothesis: Type.Optional(Type.String({ description: "对应实验假设" })),
        workaround: Type.Optional(Type.String({ description: "临时解决方案" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactIssueOpen(params);
      },
    }),

    tool({
      name: "fact_issue_resolve",
      label: "解决问题",
      description: "将问题标记为已解决。issue_key 必填。返回 JSON 字符串：{ok:true, message} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        issue_key: Type.String({ description: "问题唯一键" }),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactIssueResolve(params);
      },
    }),

    tool({
      name: "fact_exp_log",
      label: "记录实验",
      description: "记录一次实验（input→expected→actual→verdict 闭环）。name 必填。返回 JSON 字符串：{ok:true, message, name, verdict} 或 {error:...}",
      parameters: Type.Object({
        project: Type.Optional(Type.String({ description: "项目 slug 或项目目录绝对路径" })),
        name: Type.String({ description: "实验名（如 频率扫描补测）" }),
        issue_key: Type.Optional(Type.String({ description: "关联的问题键（可选）" })),
        input_config: Type.Optional(Type.String({ description: "参数/条件" })),
        expected_result: Type.Optional(Type.String({ description: "预期（对应审稿人要求）" })),
        actual_result: Type.Optional(Type.String({ description: "实际结果" })),
        verdict: Type.Optional(Type.String({ description: "conclude/partial/fail/pending（默认 pending）", enum: ["conclude", "partial", "fail", "pending"], default: "pending" })),
        run_at: Type.Optional(Type.String({ description: "运行时间" })),
        notes: Type.Optional(Type.String({ description: "备注" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactExpLog(params);
      },
    }),

    tool({
      name: "fact_journal_query",
      label: "查询期刊画像",
      description: "按名称/缩写/期刊社/ISSN/分区 查期刊画像（中央共享库）。返回 JSON 字符串：{ok:true, count, journals[]} 或 {error:...}",
      parameters: Type.Object({
        name: Type.Optional(Type.String({ description: "期刊全名（模糊匹配）" })),
        abbrev: Type.Optional(Type.String({ description: "期刊缩写（模糊匹配）" })),
        publisher: Type.Optional(Type.String({ description: "出版社（模糊匹配）" })),
        issn: Type.Optional(Type.String({ description: "ISSN（模糊匹配）" })),
        jcr_quartile: Type.Optional(Type.String({ description: "JCR 分区（模糊匹配）" })),
        limit: Type.Optional(Type.Integer({ description: "最多返回条数（1-100）", minimum: 1, maximum: 100, default: 20 })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactJournalQuery(params);
      },
    }),

    tool({
      name: "fact_journal_upsert",
      label: "写入/更新期刊画像",
      description: "写入/更新期刊画像（中央共享库，按 name 匹配）。name 必填。返回 JSON 字符串：{ok:true, message, journal_id} 或 {error:...}",
      parameters: Type.Object({
        name: Type.String({ description: "期刊全名（匹配键）" }),
        publisher: Type.Optional(Type.String({ description: "出版社" })),
        abbrev: Type.Optional(Type.String({ description: "缩写" })),
        issn: Type.Optional(Type.String({ description: "ISSN" })),
        if_year: Type.Optional(Type.String({ description: "IF 年份" })),
        if_value: Type.Optional(Type.String({ description: "IF 值" })),
        jcr_quartile: Type.Optional(Type.String({ description: "JCR 分区" })),
        abstract_format: Type.Optional(Type.String({ description: "摘要格式要求" })),
        abstract_max_words: Type.Optional(Type.Integer({ description: "摘要最大字数" })),
        imrd_required: Type.Optional(Type.Integer({ description: "是否要求 IMRD 结构（1=是）" })),
        prisma_abstract_recommended: Type.Optional(Type.Integer({ description: "是否推荐 PRISMA 摘要（1=是）" })),
        word_limit_main: Type.Optional(Type.Integer({ description: "正文字数限制" })),
        submission_url: Type.Optional(Type.String({ description: "投稿网址" })),
        notes: Type.Optional(Type.String({ description: "备注" })),
      }),
      execute: async (params: any, _config: any, _ctx: any) => {
        return handlers.hFactJournalUpsert(params);
      },
    }),
  ],
});
