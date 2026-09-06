# Stage 5 — 项目验证报告

- 验收日期:2026-09-06
- 阶段 4 发布判定:BLOCKED → 本阶段验证后 **RELEASE CANDIDATE**
- 验证范围:Python 流水线 E2E + corpus-query 插件构建/工具调用 + 文档 + 部署脚本

> 范围说明:本次项目重构对象是 **corpus Python 流水线 + corpus-query 插件**。
> fact-infra 是 2026-09-02 已独立构建完成的既有成果(TS 1.0.0,18 测试全过),
> 本报告不将其计入验证范围(PLAN.md 阶段 5 相应清单一并标注)。

---

## 1. 验证清单结果

| # | PLAN.md 清单项 | 结果 | 证据 |
|---|---|---|---|
| 1 | Python 流水线 E2E(真实 PRISMA 流程) | ✅ | `corpus/tests/e2e.sh` — init→register-raw→process-raw→query→score→group→list 全链路,临时库(不碰生产 229MB DB),mock embedding,vec0 9 条向量 |
| 2 | Plugin corpus-query 5/5 工具调用 | ✅ | `plugins/corpus-query/test/smoke.mjs` — 5 工具真实 execute(spawn 通道),临时 DB,全部返回有效结果 |
| 3 | Plugin fact-infra 19/19 | ⬜ | **既有成果,不在本次范围**(2026-09-02 已构建:TS 1.0.0 + `node --test` 18/18 全过,见 `plugins/fact-infra/PORT_REPORT.md`) |
| 4 | 文档校对 | ✅ | README/docs/ + corpus-query src/README |
| 5 | 部署脚本 | ✅ | `install.sh` — build+validate+copy(corpus-query),`--check`/`--dry-run`,离线依赖复用;实测安装成功 |
| 6 | 版本发布 v1.0.0 tag | ⏳ | 见 CHANGELOG;tag 由 L 确认后打(发布动作) |

## 2. 验证中发现的真实缺陷(8 个,全部已修)

验证不是走形式 —— 每个工具调用都暴露了生产级 bug:

| # | 位置 | 缺陷 | 影响 | 修复 |
|---|---|---|---|---|
| 1 | cli.py | `UNIFIED_CORPUS_DB` 硬编码,未接 config(违反 ARCHITECTURE §4.3 第 116 项) | 无法用临时库,配置层形同虚设 | 改为 `config.paths.corpus_db` |
| 2 | cli.py | `--embedding-mode` 缺 `mock` 显式选项 | E2E/测试被迫依赖 qwen 降级路径 | 枚举加 mock |
| 3 | cli.py | score 缺 `--score-event-id`/`--json` 参数 | **插件 score 调用必然失败**(TS 端一直传这参数,CLI 从没支持过 —— 断裂的桥) | 补参数,写 quality_evidence_v3 幂等 |
| 4 | cli.py | 45 行字面 `\n` 转义 bug | 模块级常量定义串行 | 拆分 |
| 5 | config.py | `sqlite_vec.get_loadable_path()` 不存在(正确是 `loadable_path()`) | **vec 自动探测从未成功过**(vec_ext 恒为 '') | 修正属性名 |
| 6 | chunk_query.py | 原生 `load_extension(raw vec0.so)` ABI 不兼容(undefined symbol: sqlite3__init) | get/similar 工具必崩 | 改用 `sqlite_vec.load()` python 绑定 |
| 7 | chunk_query.py | `from corpus.config import` 裸脚本运行无包上下文 | get/similar 工具 import 失败 | sys.path 自举 |
| 8 | corpus-query 插件 | 旧 `definePluginEntry(register)` → 2026.9.1 要求 `defineToolPlugin` | **`openclaw plugins validate` 拒绝**(不导出 tool metadata) | 迁移到新 API + manifest 重建 |

另:db.py schema 相对生产库漂移(缺 projects/document_groups 表、quality_evidence 旧 DDL)——E2E 建临时库时暴露,已对齐。

## 3. 覆盖度说明(诚实边界)

- **worker socket 通道**:Python 侧链路测试 `test_contract_worker.py`(6 个)验证;插件 socket 优先路径未在插件层实测(依赖 worker 已由 Python 测试覆盖,spawn 降级已实测)。
- **qwen 真实 embedding / 真实 rerank API**:未实测(需 API key + 网络;E2E 用 mock)。生产首次调用验证是发布后验收项。
- **229MB 生产 DB**:未触碰(红线)。迁移演练留给 MIGRATION.md 阶段。
- **gateway 重启 / 插件热加载**:未做(红线)。install.sh 输出明确提示。
- **fact-infra**:本次未改动其源码/构建,与验证无关。

## 4. 结论

阶段 4/5 代码与验证就绪(corpus 流水线 + corpus-query):**v1.0.0 发布候选**。打 tag 与生产上线的 gateway 操作需 L 拍板。