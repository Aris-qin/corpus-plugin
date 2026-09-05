# PORTING SPEC — hermes-fact-infra → OpenClaw 原生 tool plugin (v1)

> 执行者: Codex (ACP)。校对/安装: 阿呆 (main agent)。所有硬决策已定,不要重新发明。

## Mission Briefing

- **Global Goal**: 把 `hermes-fact-infra` v0.3.0 的 19 个 fact_* 工具移植为 OpenClaw 原生 TS 插件,行为与 Python 版对齐,测试通过,交付可 install 的包。
- **Position**: 你是移植工程师。阿呆(主 agent)负责 review + install + gateway restart。L 是所有者,不直接参与本轮。
- **Peer Snapshot**: 源码已 clone 在 `/home/node/.openclaw/workspace/projects/hermes-fact-infra/`(plugin/fact_store.py 680 行 + plugin/__init__.py 944 行 + tests/)。本机 OpenClaw 2026.8.2,Node v24.19.0,workspace 在 `/home/node/.openclaw/workspace/`。
- **Constraints**:
  1. **禁止** install 插件、改 openclaw.json、重启 gateway(阿呆做)
  2. **禁止** 引入 node:sqlite / typebox / openclaw 之外的运行时依赖(devDeps 允许 typescript + @types/node)
  3. **禁止** 网络调用、secret 硬编码
  4. 不做旧 fact_layer/fact.db 数据回填(后续单独任务)
- **Escalation Triggers**(命中就停下写进报告,不要绕):
  - `openclaw plugins build/validate` 子命令不存在或行为与下述 SOP 不符
  - `openclaw/plugin-sdk/tool-plugin` import 解析失败
  - Python 源码里发现规格未覆盖的行为分支

## 源 → 目标映射

| 源 | 内容 | 去向 |
|---|---|---|
| `plugin/fact_store.py` (680行) | SQLite 核心: 建库/迁移/CRUD/中央索引/路径解析 | `src/store.ts` (1:1 移植) |
| `plugin/__init__.py` (944行) | 19 个 handler + 参数解析 + JSON envelope | `src/tools.ts` (handler) + `src/index.ts` (tool 定义) |
| `plugin/tests/*.py` | 行为测试 | `test/*.test.ts` (node:test,核心 12-15 例) |
| `plugin/plugin.yaml` | Hermes 清单 | (丢弃 — OpenClaw 用 openclaw.plugin.json,由 CLI 生成) |

**目标目录**(SOP 标准布局,双层目录是正常的):
```
/home/node/.openclaw/workspace/agents/plugins/fact-infra/fact-infra/
├── package.json          (openclaw.extensions 指向 ./dist/index.js)
├── tsconfig.json         (NodeNext ESM, strict)
├── src/index.ts          (defineToolPlugin + 19 个 tool 定义 + TypeBox schema)
├── src/store.ts          (fact_store.py 移植)
├── src/handlers.ts       (handler 实现,保持与 Python 同名函数一一对应)
├── test/*.test.ts
└── dist/                 (tsc 产物)
```

## 硬性技术决策(已验证,勿改)

1. **SQLite**: `node:sqlite` 的 `DatabaseSync`(Node 24 已实测可用,零依赖)。不要 better-sqlite3。
2. **execute 签名是 4 参数**: `execute: async (toolCallId, params, signal, onUpdate)` — params 是第 2 个。写成 `async ({project}) =>` 是错的(头号坑)。
3. **SDK import**: `import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin"`;`import { Type } from "typebox"`。**先读参考实装** `/home/node/.openclaw/extensions/glm-web-search/` 的 import 方式,与其保持一致(若它用相对/别名利导,跟随它)。npm 依赖解析: openclaw 包在 `/app`(pnpm workspace),若 `npm install` 找不到 openclaw 模块,tsconfig `paths` 指到 `/app/dist/plugin-sdk/*.d.ts` 或 package.json `dependencies` 用 `"openclaw": "file:/app"`——选侵入最小方案并写进报告。
4. **中央索引路径**(保持 Python 的 env override 语义):
   `process.env.FACT_INDEX_DB || path.join(process.env.OPENCLAW_STATE_DIR || homedir()/.openclaw, "fact-index.db")`
5. **项目根**(Python 是 /workspace/projects,这里改): env `FACT_PROJECTS_ROOT` || `/home/node/.openclaw/workspace/projects`。`resolve_project_db` 的多级回退(slug → project_registry 查 db_path → 项目根下直接找)逻辑 1:1 保留。
6. **SQLite 细节对齐**: 每连接 `journal_mode=WAL` + `busy_timeout=30000`(Python 是 connect timeout=30);`PRAGMA user_version` 迁移阶梯 v1→v1.1→v2 单事务(BEGIN IMMEDIATE…COMMIT,失败整体 ROLLBACK)——严格按 fact_store.py `_migrate_*` 移植。
7. **表名白名单**防注入(fact_query 用)照搬。
8. **归档**: Python `ArchivedProjectPath` 语义保留(路径在 `已完成归档/` 下的项目报错并提示 rebind activate)。
9. **返回 envelope 1:1**: 工具返回 `JSON.stringify({ok:true,...})` / `{error:...}` 字符串(与 Python `_ok`/`_err` 同构),便于逐字段 diff 校对。工具描述里写清返回是 JSON 字符串。
10. **工具命名**: 全部 `fact_*` 前缀,与 Python 完全同名: fact_init, fact_status, fact_query, fact_index, fact_archive, fact_rebind, fact_task_add, fact_task_update, fact_decision_add, fact_decision_resolve, fact_issue_open, fact_issue_resolve, fact_exp_log, fact_goal_add, fact_goal_update, fact_note_add, fact_phase_set, fact_journal_query, fact_journal_upsert (共 19)。

## TypeBox schema 要求

- 从 Python handler 里逐个提取参数(`_need` = required;`args.get`/`.get(...)` 带默认 = optional),每个 tool 一张参数表写进报告: name / type / required / 说明(取自 Python docstring 或错误文案)。
- `project` 参数: optional string(Hermes 版有 cwd 回退,OpenClaw 无 cwd——**只留** "slug 或绝对路径" 两种解析,回退链见决策 5)。
- description 字段给模型看: 写触发条件 + 返回结构,中文,参考 Python docstring。

## 测试要求

- runner: `node --test`(零依赖),TS 用 tsc 编译后跑 dist 或 tsx?——**用 tsc 编译 + node --test dist/test/**,不引 tsx。
- FACT_INDEX_DB / FACT_PROJECTS_ROOT 全部指到 os.tmpdir() 下临时目录,测试互不污染,跑完清理。
- 必须覆盖(对应 pytest 用例,可合并): ① fact_init 建库+注册中央索引 ② task add/update 生命周期 ③ decision add/resolve ④ issue open/resolve ⑤ exp log ⑥ journal upsert/query(中央库) ⑦ fact_status 计数与 stale 提示 ⑧ archive → 查询报"已归档" → rebind activate 恢复 ⑨ schema 迁移: 手工造 v1 库(user_version=0 + schema_version='1')打开后自动升 v2 ⑩ fact_query 白名单拒绝非法表名 ⑪ resolve_project_db 回退链(slug/路径/不存在)。

## 交付物

1. 上述目录结构的完整可编译包,`npm run build` 通过,`openclaw plugins build --entry ./dist/index.js` + `openclaw plugins validate --entry ./dist/index.js` 通过(期望 "Plugin fact-infra is valid.")
2. `npm test` 全绿
3. **`PORT_REPORT.md`**(包根目录): ① 文件清单+行数 ② 19 工具参数表 ③ 测试结果(n passed) ④ 与 Python 版的全部偏差清单(每条带理由) ⑤ OPEN QUESTIONS(发现但没擅自决定的) ⑥ build/validate 命令原始输出摘录

## 参考路径速查

| 什么 | 在哪 |
|---|---|
| 移植源码 | /home/node/.openclaw/workspace/projects/hermes-fact-infra/plugin/ |
| OpenClaw 插件 SOP | /home/node/.openclaw/skills/openclaw-tool-plugin-engineering/SKILL.md |
| 参考实装(已在线) | /home/node/.openclaw/extensions/glm-web-search/ |
| openclaw 包(类型来源) | /app(package.json exports 有 222+ plugin-sdk 子路径;tool-plugin.js 实测存在) |
| typebox | /app/node_modules/typebox |
| openclaw CLI | `openclaw`(PATH 里有 shim → /app/dist/index.js) |
