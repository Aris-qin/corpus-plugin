# corpus-plugin

> 统一文献库 + 事实真相源工具集 — 完整项目仓库

## 概览

`corpus-plugin` 是一个独立的科研辅助工具集,提供两类核心能力:

1. **统一文献库 (Corpus)** — SQLite + sqlite-vec 向量库的科研文献检索 / 切片 / 评分 / 入库流水线
2. **事实真相源 (Fact Layer)** — 科研项目的结构化事实记录(任务/决策/问题/实验/目标/期刊画像)

通过两个 OpenClaw 原生 Tool Plugin 在 agent 运行时被调用。

## 仓库结构

```
corpus-plugin/
├── README.md                      ← 你在这里
├── LICENSE                        ← MIT
├── CHANGELOG.md
├── .gitignore
├── docs/
│   ├── PLAN.md                    ← 5 阶段项目计划(GIT → 设计 → 方案 → 代码 → 验证)
│   └── SKILL-corpus-prisma.md     ← PRISMA 7 阶段流水线 SOP
├── corpus/                        ← Python 文献库流水线
│   ├── README.md
│   ├── pyproject.toml
│   ├── cli.py                     ← 命令行入口(8 个子命令)
│   ├── db.py                      ← SQLite + sqlite-vec schema
│   ├── chunker_markdown.py        ← Markdown 切片器
│   ├── jats_to_md.py              ← JATS XML → Markdown
│   ├── openalex.py                ← OpenAlex 元数据接入
│   ├── reembed_project.py
│   ├── migrate_v2.py
│   ├── migrate_v2.sql
│   ├── a2a_wake.sh                ← A2A 接力棒 helper
│   └── tests/
└── plugins/
    ├── corpus-query/              ← Plugin #1:5 个 corpus_* 工具
    │   ├── README.md
    │   ├── package.json
    │   ├── tsconfig.json
    │   ├── openclaw.plugin.json
    │   ├── pnpm-workspace.yaml
    │   ├── pnpm-lock.yaml
    │   ├── RESULT.md              ← 验收报告
    │   ├── src/index.ts
    │   ├── chunk_query.py         ← Python helper(被 src/index.ts 调用)
    │   └── test/
    └── fact-infra/                ← Plugin #2:19 个 fact_* 工具
        ├── README.md
        ├── PORTING_SPEC.md        ← Python → TS 移植规格
        ├── PORT_REPORT.md         ← 移植报告
        ├── package.json
        ├── tsconfig.json
        ├── openclaw.plugin.json
        ├── src/
        │   ├── index.ts           ← defineToolPlugin + 19 tool 定义
        │   ├── store.ts           ← SQLite 核心(673 行)
        │   └── handlers.ts        ← handler 实现(817 行)
        └── test/store.test.ts     ← 18 个核心测试
```

## 当前状态

| 阶段 | 状态 |
|---|---|
| **GIT** — 建项目骨架 | ✅ v0.1.0 完成 |
| 产品设计 — 全面分析 corpus 功能 | ⏳ 待启动 |
| 技术方案 — plugin 工具重构架构 | ⏳ 待启动 |
| 代码书写 — 实施重构 | ⏳ 待启动 |
| 项目验证 — E2E + 文档 + 部署 | ⏳ 待启动 |

详细计划见 [`docs/PLAN.md`](docs/PLAN.md)。

## 快速开始

> ⚠️ **注意**:v0.1.0 是脚手架阶段,代码逻辑未做任何重构。安装与运行细节会在"代码书写"阶段完成后补齐。

### Python 流水线(`corpus/`)

```bash
cd corpus/
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
corpus-cli --help
```

### TypeScript Plugins

```bash
# corpus-query plugin
cd plugins/corpus-query
npm install
npm run build
openclaw plugins build --entry ./dist/index.js
openclaw plugins validate --entry ./dist/index.js
openclaw plugins install "$(pwd)"

# fact-infra plugin
cd ../fact-infra
npm install
npm run build
openclaw plugins build --entry ./dist/src/index.js
openclaw plugins validate --entry ./dist/src/index.js
openclaw plugins install "$(pwd)"
```

## 文档

- [`docs/PLAN.md`](docs/PLAN.md) — 5 阶段项目计划
- [`docs/SKILL-corpus-prisma.md`](docs/SKILL-corpus-prisma.md) — PRISMA 7 阶段流水线 SOP
- `corpus/README.md` — Python 流水线细节(下一阶段写)
- `plugins/corpus-query/README.md` — corpus-query plugin 细节(下一阶段写)
- `plugins/fact-infra/README.md` — fact-infra plugin 细节(下一阶段写)
- `plugins/fact-infra/PORTING_SPEC.md` — fact-infra Python→TS 移植规格
- `plugins/fact-infra/PORT_REPORT.md` — fact-infra 移植报告

## 协议

[MIT](LICENSE)
