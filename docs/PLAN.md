# corpus-plugin 项目计划

> **5 阶段流程**:GIT → 产品设计 → 技术方案 → 代码书写 → 项目验证
> 当前阶段:**GIT (v0.1.0) ✅ 完成**

## 项目目标

1. **全面分析**现有 corpus 功能(代码 + 工具 + 文档)
2. **重构**两个 OpenClaw Tool Plugin(`corpus-query` + `fact-infra`)的工具集
3. **统一**散落在 `workspace/scripts/corpus/`、`workspace/agents/plugins/fact-infra/`、`~/.openclaw/skills/corpus-prisma-research-pipeline/` 的资产

## 5 阶段流程

### 阶段 1 — GIT(项目脚手架) ✅

- [x] 解除 worktree 关联,建立独立 Git 仓库(`git init -b main`)
- [x] 项目骨架:`README.md` / `LICENSE` / `.gitignore` / `CHANGELOG.md`
- [x] 资产搬迁:Python 流水线 + 2 个 TS plugin + SKILL 文档
- [x] `docs/PLAN.md` 锁定 5 阶段流程
- [x] 首次 commit

### 阶段 2 — 产品设计(全面分析 corpus 功能) ⏳

**目标**:从产品视角梳理现有 corpus 的能力边界、用户场景、痛点。

**输入**:
- `corpus/` Python 流水线 (~3964 行)
- `plugins/corpus-query/` 5 个工具
- `plugins/fact-infra/` 19 个工具
- `docs/SKILL-corpus-prisma.md` PRISMA SOP
- 现有 `workspace/scripts/corpus/` 的使用记录 + lessons

**输出**:
- `docs/PRODUCT.md` — 产品设计文档
  - 现有功能全景图(模块 / 接口 / 数据流)
  - 用户场景梳理(谁用 / 用什么 / 解决什么)
  - 痛点清单(代码重复 / 路径硬编码 / 配置分散 / 文档缺失)
  - 重构价值排序(高/中/低)
  - 不做什么(out-of-scope)

### 阶段 3 — 技术方案（plugin 工具重构架构） ✅

**目标**:基于产品分析,设计技术方案。

**输入**:
- `docs/PRODUCT.md` 阶段 2 产物

**输出**:
- `docs/ARCHITECTURE.md` — 架构设计
  - 模块切分(Python ↔ Plugin ↔ Skill 边界)
  - 接口契约(OpenClaw plugin API + Python CLI 协议)
  - 数据流图(chunks/embeddings/fact.db 关系)
  - 路径管理方案(消除硬编码,引入 `corpus-cli` locator)
  - 配置统一方案(单一配置源)
  - 版本兼容策略(向后兼容旧 fact.db / corpus.db)
- `docs/MIGRATION.md` — 迁移指南
  - 从 workspace 旧路径到新项目的对应关系
  - 用户需要做的迁移动作清单（✅ 完成 2026-09-06）

### 阶段 4 — 代码书写(实施重构) ⏳

**目标**:按技术方案实施代码改动。

**输入**:
- `docs/ARCHITECTURE.md` 阶段 3 产物

**任务分类**:
- **路径硬编码修复**:所有 `/root/.openclaw/workspace/scripts/corpus/cli.py` 等改为相对路径或配置
- **代码组织重构**:Python 模块拆分 / Plugin 目录标准化 / 测试覆盖
- **接口统一**:`corpus-cli` 命令标准化 / OpenClaw plugin 工具命名规范化
- **配置统一**:从环境变量 → 配置文件(`.corpus.toml` 或类似)
- **文档同步**:每个子目录补 `README.md`

**产出**:
- v0.2.0 / v0.3.0 等版本(按重构规模分版本)

### 阶段 5 — 项目验证(E2E + 文档 + 部署) ⏳

**目标**:确保重构后整体可用、文档完整、可发布。

**清单**:
- [ ] Python 流水线 E2E 测试(至少 1 个真实 PRISMA 流程跑通)
- [ ] Plugin `corpus-query` 19/19 + 5/5 工具调用验证
- [ ] Plugin `fact-infra` 19/19 工具调用验证
- [ ] 文档校对:`README` / `docs/` / 各子目录 `README.md`
- [ ] 部署脚本:`install.sh` 或 Dockerfile
- [ ] 版本发布:`v1.0.0` tag + release notes

## 风险与依赖

| 风险 | 缓解 |
|---|---|
| 现有 corpus 流水线在生产中使用,重构破坏工作流 | 阶段 4 引入 feature flag,旧路径保留兼容期 |
| workspace skill 触发依赖原路径 | 阶段 3 同步更新 `~/.openclaw/skills/` 触发位置 |
| 2 个 plugin 各自的 npm 依赖与 OpenClaw 版本耦合 | 阶段 3 统一测试矩阵 |
| fact.db / corpus.db 数据迁移 | 阶段 4 保持 schema 不变,仅优化代码 |

## 决策日志

| 日期 | 决策 | 理由 |
|---|---|---|
| 2026-09-05 | 项目形态:本地独立仓库 | L 选定;workspace 不受影响 |
| 2026-09-05 | 代码范围:Python + TS plugin + SKILL | L 选定;完整 corpus 工具集 |
| 2026-09-05 | 阶段 1 只搬代码不改逻辑 | 防止 GIT 阶段过大,改动留到阶段 4 |
