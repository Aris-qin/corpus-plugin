# Corpus Query Tool Plugin 验收报告

- 验收日期：2026-08-16（UTC）
- 验收目录：`/root/.openclaw/workspace/scripts/corpus/plugin/`
- 最终结论：**PLUGIN_OK**

## 1. 验收摘要

本 plugin 当前完整注册并可加载 5 个 agent-callable tools：

1. `corpus_query`
2. `corpus_search_self`
3. `corpus_score`
4. `corpus_get_chunk`
5. `corpus_list_similar_level`

验收中发现并修复了 3 类 plugin 自身问题：

- `openclaw.plugin.json` 原先只声明 3 个 tool，已补齐 `corpus_get_chunk` 与 `corpus_list_similar_level`。
- `chunk_query.py get` 原先会省略显式请求但为空的 `parent` / `children` / `siblings` 字段，已改为稳定返回 `null` 或空数组。
- `chunk_query.py` 原先在 chunk 不存在时输出错误 JSON 但退出码仍为 0；已改为退出码 1，同时 TypeScript wrapper 会在非零退出时保留 helper 的 stdout 错误信息。

`dist/index.js` 已由当前 `src/index.ts` 重新编译。

## 2. Manifest 完整性

**结果：通过（修复后）**

`openclaw.plugin.json` 的 `contracts.tools` 已声明全部 5 个 tool，和 `src/index.ts` 中的 5 次 `api.registerTool(...)` 一致。

运行时检查结果：

- plugin `status`: `loaded`
- `enabled`: `true`
- `activated`: `true`
- `toolNames`: 5 个，名称完整且与 manifest 一致
- `diagnostics`: 空数组

Gateway 已读取到修复后的 manifest，因此本次无需执行 `kill -1 <gateway-pid>`。

## 3. TypeScript / TypeBox 审查

### 通用实现

**结果：通过**

- import 正确：`spawn`、`definePluginEntry`、`Type` 均可编译和加载。
- 使用的是 `Type.Enum(...)`，没有使用不存在的 `Type.StringEnum(...)`。
- `typebox@1.1.39` 运行时确认 `Type.Enum` 存在、`Type.StringEnum` 不存在。
- Python 调用使用参数数组传给 `spawn`，没有 shell 拼接和命令注入面。
- CLI 超时为 120 秒；启动失败、非零退出、非法 JSON 均会抛出明确错误。
- 非零退出现在会优先呈现 stderr，其次呈现 stdout 中的结构化 helper 错误。

### `corpus_query`

**结果：通过**

- 必填参数：`project`、`query`。
- `top_k`: integer，范围 1–50，schema 默认 10；映射 CLI `--top`，与 `cmd_query` 默认 10 一致。
- `pmid_filter`: 逗号分隔字符串；映射 CLI `--pmid-filter`，与 CLI 实现一致。
- `rerank_mode`: `Type.Enum({linear, dashscope})`；映射 CLI `--rerank-mode`。
- 参数未传时由 CLI 使用 `dashscope` 默认值，与 `cli.py` 一致。
- 功能覆盖混合召回、rerank，以及结果中的 relevance / quality / venue 维度。

### `corpus_search_self`

**结果：通过**

- 必填参数：`project`、`query`。
- `top_k`: integer，范围 1–50，schema 默认 5；映射 `--top`，与 CLI 默认 5 一致。
- `level`: integer 0–3；与 CLI `choices=[0,1,2,3]` 一致。
- `expand_context`: boolean，默认 false；true 时传 `--expand-context`。
- 调用时明确使用全局参数 `--embedding-mode qwen`，并依赖 CLI 默认 JSON 输出。
- 覆盖 self-written 文档向量召回、path boost/tree rerank 和可选上下文扩展。

### `corpus_score`

**结果：通过**

- 必填参数：`project`、`pmid`、`criterion_validity`、`outcome_reliability`、`conclusion_data_consistency`。
- 三个评分均限定在 0–1，分别正确映射到 CLI `--criterion`、`--outcome`、`--conclusion`。
- `notes` 可选并正确映射。
- CLI 文本结果解析与当前 `cmd_score` 输出格式一致。
- 该 tool 实际是文献质量证据评分的持久化更新，不是对任意 chunk 独立重算 relevance / venue；原始三维需求由 `corpus_query` 返回三维结果、`corpus_score` 单独维护 quality 维度共同覆盖。

### `corpus_get_chunk`

**结果：通过（修复后）**

- `chunk_id` 必填。
- `include` 使用 `Type.Array(Type.Enum(...))`，枚举包含 `text`、`snippet`、`metadata`、`parent`、`children`、`siblings`。
- 未传 `include` 时默认 `text + metadata`。
- 显式请求树字段时稳定返回：无父节点为 `parent: null`，无子/兄弟节点为 `children: []` / `siblings: []`。
- metadata 含 `level`、`path`、`heading_path`、`heading_chain`、`section_importance`、`doc_id`；parent/children/siblings 均含足够的树定位信息。

### `corpus_list_similar_level`

**结果：通过**

- `chunk_id` 必填。
- `top_k`: integer，范围 1–20，默认 5；helper 内也限制到 1–20。
- `same_doc`: boolean，默认 false；true 时传 `--same-doc`。
- KNN SQL 使用正确的 sqlite-vec vec0 语法：`embedding MATCH ? AND k = ?`。
- 先按向量 KNN 召回，再按相同 `level`、近似 path depth、可选同文档过滤，最后结合向量相似度和 heading-chain Jaccard 排序。

## 4. `chunk_query.py` 验收

### 数据结构

**结果：通过（修复后）**

指定命令：

```bash
python3 chunk_query.py get --chunk-id "review_ai_fall_elderly__ch2__p1" --include text,metadata,parent,children,siblings
```

实际返回：

- chunk 本文、PMID 和 metadata 正确。
- `parent: null`，符合该 H1 chunk 无父 chunk 的数据库事实。
- 返回 4 个 H2 children，ID/path/heading chain 正确。
- `siblings: []`，符合数据库中 `sibling_ids=[]` 的事实。
- 输出为合法 JSON。

### KNN 与向量格式

**结果：通过**

指定命令：

```bash
python3 chunk_query.py similar --chunk-id "review_ai_fall_elderly__ch2__p1" --top-k 5
```

命令成功执行并返回同为 level 1 的相似章节。当前库中该参考 chunk 之外只有 4 个符合条件的同级章节，因此返回 4 条而非 5 条是数据基数所致，不是执行错误。

向量存储检查：

- sqlite 返回类型：`bytes`
- blob 大小：4096 bytes
- 维度：1024 个 float32
- helper 的 `struct.pack(f"<{len(vector)}f", ...)` 与 `db.py` 完全一致，均为 little-endian float32。

### 错误行为

**结果：通过（修复后）**

不存在的 chunk 现在输出结构化错误 JSON 并返回退出码 1；plugin wrapper 会将错误内容带入异常，不再把业务错误当作成功 tool response。

## 5. 编译与加载测试

以下命令均成功：

```bash
./node_modules/.bin/tsc --noCheck
node -e "import('./dist/index.js').then(m=>console.log('ok',m.default.id))"
openclaw plugins inspect corpus-query-tool --runtime --json
```

结果：

- TypeScript 编译退出码 0。
- ESM 加载输出：`ok corpus-query-tool`。
- OpenClaw runtime 状态为 `loaded`。
- runtime `toolNames` 和 `contracts.tools` 均为完整 5 项。
- 无 runtime diagnostics。

## 6. 原始需求覆盖评估

| 原始需求 | 覆盖工具 | 结论 |
|---|---|---|
| chunk 混合召回 + rerank | `corpus_query` | 完整覆盖：向量 + keyword 混合召回，支持 linear / DashScope rerank。 |
| chunk 三维评分 relevance / quality / venue | `corpus_query` + `corpus_score` | 按当前设计覆盖：query 返回三维检索/推荐信号，score 持久化维护 quality evidence；没有单独的任意 chunk 三维重评分接口。 |
| 层状树结构与 tree rerank | `corpus_search_self` + `corpus_get_chunk` | 完整覆盖：search-self 做 path/tree rerank，get-chunk 返回明确树位置和邻接节点。 |
| 取 chunk 时知道树位置并召回相似层级 | `corpus_get_chunk` + `corpus_list_similar_level` | 完整覆盖：先取得 path/level/parent/children/siblings，再按 level + depth + 向量/标题链召回。 |

**缺失 tool：无。**

**多余 tool：无。** 5 个 tool 的职责边界清晰；将精确 chunk/tree 查询从 corpus 主 CLI 分离到只读 helper 也符合最小侵入约束。

## 7. Schema 设计评估

总体合理：

- 必填字段与 CLI required 参数一致。
- 数值边界能阻止明显无效请求。
- 枚举值与 CLI choices 一致。
- 默认值与 CLI 默认值一致，或省略后由 CLI 接管默认值。
- tool 描述基本能指导 agent 选择正确工具。

非阻断改进建议：

- 可在 `rerank_mode` schema 中显式写出 `default: "dashscope"`，让 agent-facing schema 更直观；当前省略参数仍会正确使用 CLI 默认值。
- 可为 `top_k` 增加更具体的 description，而不只提供范围和 default。
- `same_doc=false` 的语义实际是“不限制文档”，不是“排除同文档、只跨文档”；现有描述可进一步避免歧义。

## 8. 错误处理与性能评估

### 错误处理

阻断问题已修复，当前可接受：

- Python 进程无法启动：抛错。
- Python 非零退出：抛错并携带 stderr/stdout 详情。
- CLI/helper 返回非法 JSON：抛错并附前 500 字符输出。
- chunk 不存在或无向量：helper 返回结构化错误并退出 1。
- score 输出格式异常：拒绝静默解析，抛出 unexpected response。

非阻断风险：

- `runScoreCli` 依赖当前 CLI 单行文本格式；CLI 文案变化会导致解析失败。长期可考虑让 `cmd_score` 支持 JSON，但本次约束禁止修改 corpus CLI。
- 绝对路径绑定 workspace 和全局 sqlite-vec 扩展位置，迁移安装目录时需要同步修改；在当前部署环境中可正常工作。
- stdout/stderr 未设置大小上限；当前查询规模和 top-k 上限下风险较低。

### 性能

当前实现没有明显阻断性能问题：

- 每次 tool 调用会启动一个 Python 子进程，存在固定启动开销，但实现简单、隔离清晰，适合当前低并发 agent tool 场景。
- similar-level KNN 使用 `max(top_k * 4, 40)` over-fetch，再做 level/depth 过滤；数据库调用成功且当前样例延迟很低。
- helper 对每个 KNN 候选单独查询 metadata，形成最多约 40–80 次的小型 SQLite lookup；当前规模可接受，高并发或大 top-k 时可改为批量 `IN (...)` 查询。
- post-filter 可能在同层级候选稀少时少于 `top_k`，这是 best-effort 语义；若未来要求严格填满，可逐步扩大 KNN pool，但不应在本次验收中重写算法。

## 9. 最终结论

**PLUGIN_OK**

修复后，manifest、源码注册、编译产物和 OpenClaw runtime 均一致地暴露 5 个工具；TypeBox schema 与 corpus CLI 参数匹配；chunk tree helper、sqlite-vec KNN 和 little-endian float32 处理均通过真实数据库测试。原始 4 类需求均得到覆盖，没有必须新增或删除的 tool。

## 10. 主 agent 功能测试(2026-08-16 22:04,补充)

SKILL.md 更新后(4 处:架构总览 / PRISMA 7.5 / Plugin 接口章节 / 决策点 13+14),通过模拟 agent 调用(加载 dist 的 register → 拿 5 个 tool → 逐个 execute)验证:

| Tool | 参数 | 结果 |
|---|---|---|
| corpus_query | project=ar-review, query="EGFR degradation", top_k=3 | ✅ 3 results,首条 "The immune system in cardiovascular diseases..." |
| corpus_search_self | project=review-ai-fall-elderly, query="risk stratification", top_k=3, level=1 | ✅ 1 result, review_ai_fall_elderly__ch2__p1 score=0.9902 path=/ch2 |
| corpus_score | project=ar-review, pmid=16048553, criterion=0.8/outcome=0.7/conclusion=0.6 | ✅ quality_final=0.7 evidence_mean=0.7 |
| corpus_get_chunk | chunk_id=review_ai_fall_elderly__ch2__p1, include=metadata,children | ✅ pmid=selfw__..., level=1, children=4 |
| corpus_list_similar_level | chunk_id=review_ai_fall_elderly__ch2__p1, top_k=3 | ✅ 3 results |

**注意**(数据事实,非 bug):
- ar-review 项目没有 self_written 文档(在 review-ai-fall-elderly)→ search_self 用错项目会报 "no self_written chunks"
- score 的 pmid 必须在项目 document_groups 里,否则 CLI 会 auto-register(属 CLI 既有行为)

**连接状态**:plugin status=loaded, enabled=true, toolNames 5 个,无 diagnostics。
