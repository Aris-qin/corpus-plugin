# fact-infra 移植报告 (Python → OpenClaw TS)

## 1. 文件清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `src/store.ts` | 673 | SQLite 核心（建库/迁移/CRUD/中央索引/路径解析），1:1 移植 fact_store.py |
| `src/handlers.ts` | 817 | 19 个 handler 实现，保持与 Python 同名函数一一对应 |
| `src/index.ts` | 300 | defineToolPlugin + 19 个 tool 定义 + TypeBox schema |
| `test/store.test.ts` | 343 | 18 个核心测试用例，覆盖必需场景 |
| `package.json` | 43 | npm 包配置，零运行时依赖（仅 typebox） |
| `tsconfig.json` | 12 | TypeScript 编译配置（NodeNext ESM） |
| **总计** | **2188** | 编译产出 dist/src/*.js (2133 行) |

## 2. 19 工具参数表

| 工具名 | 必填参数 | 可选参数 | 说明 |
|---|---|---|---|
| `fact_init` | project_dir, slug, project_type | name | 初始化项目 fact.db 并注册到中央索引 |
| `fact_status` | - | project | 查看项目状态总览（计数+动态+phase+goals） |
| `fact_query` | project | table, goal_id, limit | 查询表内容或 goal 明细 |
| `fact_index` | - | category, status | 中央索引总览（所有项目+期刊） |
| `fact_rebind` | project | db_path, activate | 修复失联路径 / 重新激活归档项目 |
| `fact_archive` | project | force | 标记项目为 archived |
| `fact_goal_add` | project, title, done_criteria | parent_goal_id, phase, position | 创建目标 |
| `fact_goal_update` | project, goal_id | status, title, done_criteria, phase, confirm | 更新目标（闭环检查） |
| `fact_phase_set` | project, phase | confirm | 设置项目当前阶段 |
| `fact_note_add` | project, text | goal_id, refs | 记录进展笔记 |
| `fact_task_add` | project, title | type, priority, notes | 记录任务 |
| `fact_task_update` | project, task_id | status, title | 更新任务进度 |
| `fact_decision_add` | project, title | decision_key, summary, rationale, source_file | 记录决策+理由 |
| `fact_decision_resolve` | project, decision_key | status | 更新决策状态 |
| `fact_issue_open` | project, title | issue_key, severity, hypothesis, workaround | 记录问题 |
| `fact_issue_resolve` | project, issue_key | - | 标记问题已解决 |
| `fact_exp_log` | project, name | issue_key, input_config, expected_result, actual_result, verdict, run_at, notes | 记录实验 |
| `fact_journal_query` | - | name, abbrev, publisher, issn, jcr_quartile, limit | 查询期刊画像 |
| `fact_journal_upsert` | name | publisher, abbrev, issn, if_year, if_value, jcr_quartile, abstract_format, abstract_max_words, imrd_required, prisma_abstract_recommended, word_limit_main, submission_url, notes | 写入/更新期刊画像 |

## 3. 测试结果

```
✔ fact-infra store (63.673734ms)
ℹ tests 18
ℹ suites 1
ℹ pass 18
ℹ fail 0
```

**覆盖场景：**
1. ✅ fact_init 建库+注册中央索引
2. ✅ task add/update 生命周期
3. ✅ decision add/resolve
4. ✅ issue open/resolve
5. ✅ exp log
6. ✅ journal upsert/query（中央库）
7. ✅ fact_status 计数与 stale 提示
8. ✅ archive → 查询报"已归档" → rebind activate 恢复
9. ✅ schema 迁移: 手工造 v1 库(user_version=0 + schema_version='1')打开后自动升 v2
10. ✅ fact_query 白名单拒绝非法表名
11. ✅ resolve_project_db 回退链(slug/路径/不存在)
12. ✅ task_update 不存在的 task_id 报错
13. ✅ decision_resolve 不存在的 decision_key 报错
14. ✅ issue_resolve 不存在的 issue_key 报错
15. ✅ goal add 和 query detail
16. ✅ phase_set 守卫：active goals 需 confirm
17. ✅ goal_update 闭环检查：未收尾项需 confirm
18. ✅ note_add refs 验证

## 4. 与 Python 版的偏差清单

| 项 | Python 行为 | TS 行为 | 理由 |
|---|---|---|---|
| **中央索引路径** | `~/.hermes/fact-index.db` | `~/.openclaw/fact-index.db` 或 `$OPENCLAW_STATE_DIR/fact-index.db` | 符合 OpenClaw 规范（规格书决策 4） |
| **项目根默认路径** | `/workspace/projects` | `/home/node/.openclaw/workspace/projects` | 适配 OpenClaw workspace 布局（规格书决策 5） |
| **cwd 回退** | 支持 `cwd` 参数（Hermes 传入工作目录） | **不支持** `cwd`，只保留 slug 或绝对路径解析 | OpenClaw 无 cwd 上下文（规格书决策 5 明确） |
| **SQLite 连接** | `sqlite3.connect(timeout=30)` | `DatabaseSync` + `PRAGMA busy_timeout=30000` | Node 24 `node:sqlite` API 对应（规格书决策 1） |
| **线程本地连接** | `threading.local()` | `Map<string, DatabaseSync>` | TS 无 threading.local，用 Map 缓存 |
| **timestamp 解析** | `datetime.strptime` | `new Date()` | JS Date 原生支持 ISO 8601 |
| **execute 签名** | `(args, **kw)` | `(toolCallId, params, signal, onUpdate)` 4 参数 | OpenClaw tool execute 签名（规格书决策 2） |
| **返回值** | JSON 字符串 `{"ok":true,...}` / `{"error":...}` | **完全一致** | 1:1 对齐，便于逐字段 diff |
| **project_type** | 直接在 handler 校验 | TypeBox enum 约束 + handler 再校验 | TS 类型安全优势 |
| **table 白名单** | 硬编码列表 | 硬编码列表 + TypeBox enum | 防注入双重保护 |

## 5. OPEN QUESTIONS

**Q1: devDependencies 中的 `openclaw` 包解析**
- **现状**: `openclaw` 在 `devDependencies` 中，`npm install` 时会拉取完整依赖树（包括 typescript）
- **问题**: `peerDependencies` 中也声明了 `openclaw >=2026.5.17`，运行时 OpenClaw gateway 会提供
- **方案**: 保持当前配置（dev 时可编译，runtime 由 OpenClaw 提供）
- **建议**: 阿呆校对时确认 peerDeps 解析正常

**Q2: `typebox` 版本**
- **现状**: `^1.1.38`（参考 glm-web-search）
- **问题**: OpenClaw 内部可能用更新版本
- **方案**: 当前版本与参考实装一致，编译和验证通过
- **建议**: 如 OpenClaw 升级 typebox 到 2.x，需同步

**Q3: `FACT_PROJECTS_ROOT` 环境变量**
- **现状**: 默认 `/home/node/.openclaw/workspace/projects`
- **问题**: 是否应读取 OpenClaw 配置中的 workspace 路径？
- **方案**: 当前用 env override 模式（测试友好），可后续接入 OpenClaw config API
- **建议**: 阿呆确认是否需要读 `openclaw config get workspace.path`

**Q4: 旧数据回填**
- **规格书明确**: "不做旧 fact_layer/fact.db 数据回填(后续单独任务)"
- **现状**: 未实现
- **建议**: L 决定是否启动回填任务

## 6. build/validate 命令原始输出

```bash
$ openclaw plugins build --entry ./dist/src/index.js
Wrote openclaw.plugin.json
Updated package.json

$ openclaw plugins validate --entry ./dist/src/index.js
Plugin fact-infra is valid.
```

**生成的 manifest 摘录（openclaw.plugin.json）:**
- `id`: "fact-infra"
- `name`: "Fact Infrastructure"
- `version`: "1.0.0"
- `contracts.tools`: 19 个工具名（fact_init, fact_status, ..., fact_journal_upsert）

## 7. 已知限制 / 后续工作

1. **未实装功能**（规格书明确排除）:
   - 旧 fact_layer/fact.db 数据回填

2. **测试覆盖**（已满足规格书要求）:
   - ✅ 核心 12-15 例（实际 18 例）
   - ✅ 行为对齐验证
   - ⚠️ 未做压力测试（并发写入、大数据量）

3. **文档**（规格书未要求，建议补充）:
   - 工具使用示例
   - 迁移指南（从 Hermes 迁移）

4. **性能优化**（后续可做）:
   - 连接池管理
   - 批量写入优化

## 8. 交付清单

- ✅ 目标包编译通过（`npm run build`）
- ✅ `openclaw plugins validate` 通过
- ✅ `npm test` 全绿（18/18）
- ✅ PORT_REPORT.md（本文件）
- ✅ 文件清单 + 行数统计
- ✅ 19 工具参数表
- ✅ 测试结果摘要
- ✅ 与 Python 偏差清单（每条带理由）
- ✅ OPEN QUESTIONS 列出
- ✅ build/validate 命令原始输出

**Escalation Triggers 检查：**
- ✅ `openclaw plugins build/validate` 子命令存在且行为符合 SOP
- ✅ `openclaw/plugin-sdk/tool-plugin` import 解析成功
- ✅ Python 源码行为已全部覆盖（无未知分支）

**下一步（阿呆执行）：**
1. `openclaw plugins install "$(pwd)"` 安装插件
2. `chmod -R go-w ~/.openclaw/extensions/fact-infra` 修复权限
3. `openclaw gateway restart` 重启网关
4. `openclaw plugins inspect fact-infra --runtime` 验证加载
5. 端到端测试：在 OpenClaw 会话中调用 fact_init 等工具

---

**移植完成时间**: 2026-09-02  
**移植工程师**: Codex (ACP)  
**规格书版本**: PORTING_SPEC.md v1  
**源码版本**: hermes-fact-infra v0.3.0 (680 + 944 行)  
**目标版本**: fact-infra 1.0.0 (2188 行 TS 源码)
