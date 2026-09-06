# Changelog

All notable changes to corpus-plugin are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-09-06

### Added
- 阶段 5 验证产物:Python 流水线 E2E(`corpus/tests/e2e.sh`,临时库全链路)、插件工具冒烟测试(`plugins/corpus-query/test/smoke.mjs` 5/5、`plugins/fact-infra/test/smoke.mjs` 19/19)、worker socket 链路测试 6 个
- 部署脚本 `install.sh`(build+validate+copy,`--check`/`--dry-run`,离线依赖复用)
- 文档:`docs/review/stage5-verification-2026-09-06.md` 验收报告;两个插件 `src/README.md`

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
