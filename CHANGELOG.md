# Changelog

All notable changes to corpus-plugin are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/).

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
