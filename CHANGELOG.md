# Changelog

All notable changes to corpus-plugin are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/).

## [2.2.0] - 2026-11-12

### Added
- `corpus_core/vector_cache.py`：离线构建 float32 二进制向量产物（mmap 零拷贝、单点可替换 dot kernel、有界 LRU），JSON 路径保留为降级回退，不引入 numpy 依赖。
- `corpus_core/backfill.py` 与 CLI `vectors-build` / `vectors-verify` / `reconcile-embedding`：显式预算的向量回填、对账与产物校验。
- 六工具参数 schema 扁平化（`contracts.schema_union`），工具目录可直接看到每个 action 的 properties 与必填项，不再只显示空参数。

### Changed
- 截断判定改为只回答“是否可能丢掉进入本页的候选”，被丢弃候选以融合分上界 `2/(60+budget)` 比较，不再拿扫描量与保留上限对比。
- RRF 名次只在裁剪后的保留榜内计算，恢复 v2.0.0 的排序语义，避免深尾名次打破精确平局并随库增长漂移。

### Fixed
- 库内向量超过保留上限后每次查询都误报 `candidate_budget_exhausted`（`status` 实为完整仍标 truncated）。
- embedding 429 单独归类为可重试的 `PROVIDER_RATE_LIMITED`，不再并入 5xx 分支。

### Safety and upgrade limitations
- 正式报告发布、批量正式采用、合并、永久删除仍默认禁用，可信真人审批未接通。
- `legacy_vectors` 保持隔离、未认领；旧原件托管与部分历史关系仍需显式接管。
- 二进制向量产物属派生物，重新嵌入导致指纹变化即失效并自动回退 JSON。
- 运行时按调用新建子进程，进程内缓存不跨调用复用。

## [2.0.0] - 2026-11-11

### Added
- Hermes 六工具全局资料库、schema v4、原件/版本/切片追溯与调研草稿。
- 显式预算、partial 索引安全恢复、DashScope 原生 rerank、有界工作集全局召回。
- 源码/sdist/wheel 回归矩阵和进程中断恢复压测。
- 专用解释器适配入口及明确目标、拒绝覆盖的安装脚本。

### Safety and upgrade limitations
- 正式发布、批量正式采用、合并、永久删除仍默认禁用，可信真人审批未接通。
- 旧向量来源与原件托管需单独接管；禁止把保真迁移称为自动语义检索接管。
- 显式 runner 与 operation_id 预算授权，不自动索引或全库重建。
- 旧 OpenClaw 代码保留，原安装脚本归档至 docs/install-v1-legacy.sh。

## [1.0.0] - 2026-09-06

### Added
- 阶段 5 验证产物:Python 流水线 E2E(`corpus/tests/e2e.sh`,临时库全链路)、插件工具冒烟测试(`plugins/corpus-query/test/smoke.mjs` 5/5)
- 部署脚本 `install.sh`(build+validate+copy,`--check`/`--dry-run`,离线依赖复用)
- 文档:`docs/review/stage5-verification-2026-09-06.md` 验收报告;corpus-query 插件 `src/README.md`

### Changed
- corpus-query 插件迁移到 `defineToolPlugin` API(OpenClaw 2026.9.1 契约),manifest 重建
- `cli.py`:`UNIFIED_CORPUS_DB` 改由 `config.paths.corpus_db` 解析(消除硬编码);score 新增 `--score-event-id`/`--json`(写 quality_evidence_v3,对齐 worker 协议);`--embedding-mode` 增加显式 `mock`;修复模块级 `\n` 字面转义
- `config.py`:修复 `sqlite_vec.get_loadable_path()` → `loadable_path()`(vec 自动探测此前从未生效)
- `chunk_query.py`:vec0 改由 `sqlite_vec.load()` python 绑定加载(原生 load_extension ABI 不兼容);sys.path 自举支持裸脚本运行
- `db.py`:schema 对齐生产库(补 `projects`/`document_groups` 表,`quality_evidence` 复合主键)

### Fixed
- 阶段 5 验证发现并修复 8 个真实缺陷(见 `docs/review/stage5-verification-2026-09-06.md` §2)

## [0.1.0] - 2026-09-05

### Added
- 独立 Git 仓库建立,从 `workspace/scripts/corpus/`、`workspace/agents/plugins/fact-infra/`、`~/.openclaw/skills/corpus-prisma-research-pipeline/` 集中搬运
- 项目骨架:`README` / `LICENSE` (MIT) / `.gitignore` / `CHANGELOG` / `docs/PLAN.md`
- Python 流水线(`corpus/`):cli.py / db.py / chunker_markdown.py / jats_to_md.py / openalex.py / reembed_project.py / migrate_v2.py / migrate_v2.sql / a2a_wake.sh + 测试
- Plugin `corpus-query`(原 `scripts/corpus/plugin/`):5 个 corpus_* 工具,基于 Python CLI 桥接
- Plugin `fact-infra`(原 `agents/plugins/fact-infra/`):19 个 fact_* 工具,SQLite + TypeBox schema
- 文档:`docs/PLAN.md`(5 阶段流程)、`docs/SKILL-corpus-prisma.md`(PRISMA SOP)

### Notes
- 这是项目脚手架阶段,代码逻辑未做任何重构(留待后续阶段)
- 旧路径硬编码(如 `/root/.openclaw/workspace/scripts/corpus/cli.py`)保留,**下一阶段统一修改**
- 历史 `.bak` 快照文件不进入版本控制(`chunker_markdown.v1.py.bak`、`cli.py.bak-pre-qwen-rename-133238`)
- 测试 DB 与 `nanorobot_demo/` 测试数据被 `.gitignore` 排除
