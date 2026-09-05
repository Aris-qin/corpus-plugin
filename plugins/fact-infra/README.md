# plugins/fact-infra

> 19 个 fact_* 工具的 OpenClaw TS Plugin,SQLite + TypeBox schema 实现

> ⚠️ **v0.1.0 脚手架阶段**:此 README 描述现有功能,不代表最终设计。

## 工具清单(19 个)

### 项目管理
- `fact_init` — 初始化项目 fact.db
- `fact_status` — 项目状态总览
- `fact_query` — 通用查询
- `fact_index` — 中央索引总览
- `fact_archive` — 标记项目 archived
- `fact_rebind` — 修复失联路径

### 目标管理
- `fact_goal_add` / `fact_goal_update`
- `fact_phase_set`
- `fact_note_add`

### 任务
- `fact_task_add` / `fact_task_update`

### 决策
- `fact_decision_add` / `fact_decision_resolve`

### 问题
- `fact_issue_open` / `fact_issue_resolve`

### 实验
- `fact_exp_log`

### 期刊画像
- `fact_journal_query` / `fact_journal_upsert`

## 文件结构

```
plugins/fact-infra/
├── README.md                  ← 你在这里
├── PORTING_SPEC.md            ← Python → TS 移植规格
├── PORT_REPORT.md             ← 移植报告(2026-09-02)
├── openclaw.plugin.json
├── package.json
├── tsconfig.json
├── package-lock.json
├── src/
│   ├── index.ts               ← 320 行,defineToolPlugin + 19 tool 定义 + TypeBox schema
│   ├── store.ts               ← 753 行,SQLite 核心(1:1 移植 fact_store.py)
│   └── handlers.ts            ← 959 行,19 个 handler
└── test/
    └── store.test.ts          ← 459 行,18 个核心测试
```

## 架构

- **项目库**: `<project_dir>/fact.db` — 每个项目独立的 SQLite 数据库
- **中央索引**: `~/.openclaw/fact-index.db` — 跨项目共享的 journals + project_registry
- Schema v2,用 `PRAGMA user_version` 管理迁移,自动从 v1 升级
- SQLite 用 Node 24 内置 `node:sqlite`(零运行时依赖)

## 安装

```bash
cd plugins/fact-infra
npm install
npm run build
openclaw plugins build --entry ./dist/src/index.js
openclaw plugins validate --entry ./dist/src/index.js
openclaw plugins install "$(pwd)"
chmod -R go-w ~/.openclaw/extensions/fact-infra
openclaw gateway restart
```

验证:
```bash
openclaw plugins list | grep fact-infra
# 应该看到 fact-infra 列出 19 个工具
```

## 测试

```bash
npm test    # node:test,18 个核心用例
```

## 已知问题

1. **项目根路径硬编码**:`FACT_PROJECTS_ROOT` env 默认为 `/home/node/.openclaw/workspace/projects`
2. **中央索引路径硬编码**:`process.env.FACT_INDEX_DB || ...`
3. **移植自 Python**:Python 版的边界行为需要在 TS 端验证完整

## 相关文档

- `PORTING_SPEC.md` — 移植规格(2026-08-30)
- `PORT_REPORT.md` — 移植报告(2026-09-02)
- 父项目来源: `hermes-fact-infra v0.3.0`(Python 版)
