# corpus-plugin 产品设计技术审查报告

审查日期：2026-09-05  
审查范围：`docs/PRODUCT.md`、`docs/PLAN.md`、`README.md`、`docs/SKILL-corpus-prisma.md`，以及 `corpus/db.py`、`corpus/cli.py`、`plugins/corpus-query/src/index.ts` 中与设计直接相关的实现。  
结论等级：条件通过（可进入阶段 3 技术方案，但必须先锁定数据契约、评分版本和验证门槛）。

## 总体结论

四支柱（解析、结构切片存储、位置感知召回、质量评分）覆盖了个人文献库的主闭环，个人轻量化取舍总体合理。当前最大风险不是功能缺失，而是“设计决策已更新、代码和 schema 尚未同步”：PRODUCT 已采用 0.7/0.3 新公式，但 `db.py` 仍按旧 `max(prior,evidence)` 计算；PLAN/README 仍把项目停留在脚手架阶段；`process-pdf` 仍是单格式入口。

在阶段 3 开始前，应把以下事项列为阻断条件：

1. 以版本化评分契约统一 `quality_evidence`、推荐阈值和旧数据迁移行为。
2. 以 Python 服务或 Python worker 作为首选在线召回边界，暂不引入 native Node 依赖。
3. 为每个输入格式定义规范化中间表示（canonical Markdown/tree）和结构不变量。
4. 为重切片、失败重试、向量孤儿、增量 embedding 建立可恢复的状态机。
5. 阶段 5 增加离线基准集、回归阈值、数据一致性检查，而非只做“跑通一次 E2E”。

## 1. 产品定位与四支柱

### 结论

划分合理，且比 RAGFlow 全栈功能更适合单用户、CLI/agent 工作流。砍掉 Web UI、图谱、多租户、分布式搜索和 Docker 不构成当前刚需缺口；但“可观测性/可修复性”不是被砍功能，必须作为横切能力补回设计。引用脉络暂由 researcher 工作流承担也合理，但要明确其不参与 corpus 排序和一致性保证。

### 理由

- Ingest→Chunk/Index→Retrieve→Quality 与现有 CLI 分组、`documents/chunks/chunk_vectors/relevance/quality_evidence` 表基本对应。
- `corpus_get_chunk` 提供按需树上下文，符合轻量默认召回，避免每次返回 parent/sibling/children 的 token 和延迟成本。
- 单用户场景下权限、多租户、ES 运维成本明显高于收益；图谱对以语义检索为主的文献问答不是成立前提。

### 风险与建议

1. **缺少运行可观测性支柱。** 没有 ingest job、解析警告、embedding 失败、索引健康和数据版本的统一状态，出错时 agent 只能看到半成品。建议增加 `ingestion_jobs` 或等价 manifest：状态（discovered/parsed/chunked/embedded/indexed/failed）、错误、重试次数、输入 hash、parser/chunker/embedder 版本。
2. **缺少人工纠错闭环。** 无 UI 不等于不能修复；至少提供 CLI 查看/标记坏 chunk、重跑单文档、导出抽样报告的能力。
3. **引用工作流边界要写死。** researcher 通过 PubMed elink 得到的引用关系应存为外部工作流产物（含抓取时间和来源），不能隐式混入 corpus 的相关度或 quality，避免排序语义漂移。
4. **非目标与已拍板格式有轻微冲突。** Non-Goals 仍写“PDF 为主、通用文档不追求”，但决策已明确 PDF/DOCX/HTML/JATS/MD。建议改成“文献相关格式”，并列出不支持扫描 OCR、PPT/Excel 等边界。

## 2. 评分公式与阈值

### 结论

`quality = 0.7×evidence_mean + 0.3×prior` 比 `max` 更能让 curator 纠偏，方向正确；但它仍是启发式，必须处理缺失维度、评分者偏差、项目间重复评分、公式版本和阈值校准。现有实现仍是 `max`，这是设计与实现不一致的 P0 风险。

### 理由

- 新公式把 prior 变为弱先验，避免类型标签抬高一篇证据不佳的文章。
- 无 curator 评分 fallback 到 prior，能覆盖 cold-start，但会让未审文献与已审文献在排序中混杂。
- `primary` 需 relevance≥0.75、`supporting` 需 relevance≥0.50 且 quality≥0.55；新公式会把大量 medium prior 文献压到 0.55 以下，推荐分布必然变化，不能沿用旧 pilot 的“完全一致”结论。

### 风险与建议

1. **冷启动可见性风险。** fallback 结果应带 `quality_status=prior_only`、`quality_confidence`，排序时可设置同分优先“已审”，但不能把 prior-only 伪装为证据确认。
2. **缺失项处理。** 三项 evidence 不能简单把缺失当 0 或静默取均值；存 `evidence_n`，少于 3 项时降低置信度，建议默认不允许 primary，除非明确人工 override。
3. **curator 偏差。** 固定评分锚点、双评者抽样、按评者校准（均值/方差或 z-score）和抽样复核；保留 `scorer_id`、`rubric_version`、`scored_at`、`reason`。
4. **权重不可硬编码。** 建议表中保存 `formula_version`、`prior_weight`、`evidence_weight`，新公式上线时可重算和审计。
5. **阈值交互。** 用标注集画 precision/recall、PR 曲线，分别校准 0.50/0.75 relevance 与 0.55 quality；把推荐定义为策略层，不要写死在数据层。
6. **排序耦合。** 当前 query 结果主要按 relevance 排序，quality 只影响 recommendation。若产品目标是“优先采信高质量”，应明确是否采用二级排序/质量 boost，并防止高质量低相关文献压过高相关证据。

## 3. 架构与在线召回候选 A/B/C

### 结论

推荐 **A：常驻 Python worker + 受控 IPC**，Python 继续拥有 sqlite-vec 连接和召回逻辑；Node 插件只做协议适配。短期可保留 **C：spawn python3** 作为兼容/降级路径；不推荐 **B：better-sqlite3** 作为当前默认方案。

### 理由

- Node 24 原生 `node:sqlite` 禁止 `loadExtension`，无法直接加载 vec0.so，纯 TS 直连在约束下不可行。
- A 避免每次 spawn 的 Python 启动、模型/数据库初始化和并发抖动，适合在线 query；同时不引入 native Node ABI 风险。
- C 依赖最少、故障隔离好，适合低 QPS个人库和灾备，但 5 个工具并发调用时启动成本、超时和 stderr 解析脆弱。
- B 可能获得同步 API 和较低启动延迟，但 native 编译/ABI、sqlite-vec 兼容、安装迁移和 OpenClaw 构建环境风险高。

### 具体建议

1. 定义稳定 JSONL/Unix socket 协议：request_id、命令、schema_version、超时、错误码、stderr 截断策略；禁止插件解析人类可读日志。
2. worker 每次事务只提交完整结果；连接断开后由插件按 request_id 重试一次，查询必须幂等。
3. 设计进程健康检查、空闲退出、数据库锁等待上限；写操作串行，读操作可排队。
4. C 路径保留 `CORPUS_QUERY_MODE=spawn` feature flag，作为 worker 不可用时的明确降级，不要两套逻辑长期漂移。
5. 向量扩展加载、维度、距离度量和 sqlite-vec 版本在启动时自检并写入诊断结果。

## 4. DOCX/HTML 多格式扩展

### 结论

把入口从 `process-pdf` 扩展为多格式是正确方向，但不能假设 Docling 输出结构与 PDF 相同。必须先建立 canonical tree，再交给 tree-chunker；否则 heading、表格、列表、脚注和分页噪声会造成 chunk 边界和路径不稳定。

### 具体风险

- DOCX 可能把样式名而非语义 heading 输出，存在跳级、重复标题、空段落和列表嵌套。
- HTML 有导航栏、广告、脚本、隐藏节点、重复 header/footer；标题可能由 CSS class 或 ARIA 角色表达。
- 表格/公式/图片 alt 文本的结构表示不同，直接拼 Markdown 会改变 token 密度和 embedding 语义。
- Docling 版本升级可能改变节点类型或 heading 识别，导致同一文档重处理后 chunk ID 大面积变化。

### 建议

1. 入口命名 `process-document --format auto|pdf|docx|html|jats|md`，保留 `process-pdf` 兼容别名。
2. 定义 canonical schema：节点类型、heading level/path、正文、table、formula、caption、source locator、parser warnings；chunker 只依赖该 schema。
3. 对 heading level 做规范化（首个 H1、跳级压缩、空标题剔除），对 HTML 做可配置 selector/黑名单清洗。
4. 为每种格式建立 golden fixtures，断言标题树、chunk 数、最大长度、表格/公式不丢失和 source locator 可回溯。
5. chunk ID 应包含文档 content hash、规范化路径和 chunker 版本；重处理不要静默覆盖旧版本。

## 5. 数据一致性与 re-chunk

### 结论

当前 `chunks` 与 `chunk_vectors` 是双表写入，`vec0` 又有特殊删除/插入限制；这是最高数据一致性风险。`relevance` 按 chunk/query 保存，`quality_evidence` 按文献/项目保存，documents 元数据变化时也会产生陈旧派生数据。必须采用版本化、事务化和可重建设计。

### 具体风险与建议

1. **向量孤儿。** 每次写入后校验 chunks↔vec0 一一对应；提供 `fsck`，检测缺失、重复、维度错误和 orphan vector。
2. **重切片级联。** 文档 content hash 或 parser/chunker 版本变化时，先建立新 generation，完成 embedding/index 后原子切换 active_generation，再异步清理旧代。
3. **相关度陈旧。** `relevance` 应包含 query 策略/embedding 版本和 chunk generation；re-chunk 后全部标记 stale，不得复用旧分数。
4. **质量同步。** quality 依赖 documents 的 prior 和 curator 输入；documents.article_type/prior 修改时，标记 quality_evidence stale，按 `formula_version` 重算。
5. **外键与删除。** 开启 foreign_keys，明确 document 删除、项目解绑和共享文献的级联策略；不要因单项目移除而删全局 raw/chunks。
6. **事务边界。** chunks、向量、generation manifest 的提交应原子；embedding API 失败只能留下可重试 job，不得留下“已索引”状态。

## 6. 阶段 5 可验证性与量化验收

### 结论

PLAN 当前“至少跑通一个真实 PRISMA 流程、工具调用验证”不足以证明召回质量或评分有效性。应增加固定数据集、人工标注、性能和一致性门槛，并把失败样例纳入回归。

### 建议验收矩阵

| 类别 | 方法 | 建议门槛 |
|---|---|---|
| 解析 | PDF/DOCX/HTML/JATS/MD 各≥10份 golden fixtures | 标题树准确率≥95%；关键表格/公式保留率≥95%；0 个 silent failure |
| 切片 | 人工标注论点边界 + 自动不变量 | 语义完整性通过率≥95%；超长 chunk≤5%；chunk tree 无孤儿 |
| 召回 | ≥50 条查询、每条≥2 名标注者 | Recall@10≥0.85，MRR@10≥0.70；primary 的 precision≥0.80 |
| 位置感知 | 随机抽样 query→get_chunk | heading_path/source locator 可回溯率 100%；上下文关系无跨文档错误 |
| 评分 | ≥100 篇双评 gold set | 与共识分 Spearman ρ≥0.70；primary precision≥0.80；按格式/类型分层报告 |
| 冷启动 | 未评分文献集 | 明确标记 prior_only；不得出现无证据的“已审核”标签 |
| 一致性 | 注入失败、重复、重试、re-chunk | fsck 0 orphan；重试幂等；切换期间查询只见完整 generation |
| 性能 | 单机基准，冷/热进程 | worker P95 查询延迟、spawn 基线、并发 1/4/8 分别记录并设回归上限 |
| 成本 | embedding 调用计数与缓存命中 | 相同 content hash 不重复计费；失败重试有上限和可恢复队列 |

验收报告必须保存查询集、标注指南、版本（parser/chunker/embedder/formula）、原始结果和失败样例，避免只报一个总体平均数。

## 7. 设计遗漏与优先级

### P0（阶段 3 前补齐）

- 评分公式已拍板但源码/文档仍旧公式，需迁移脚本、formula_version 和回算策略。
- 多格式 canonical schema、输入 hash、generation 和 re-chunk 原子切换。
- Python worker/IPC 契约、C 降级路径、并发与超时语义。
- 数据库备份/恢复：在线一致性快照、schema 版本、vec0 重建步骤、恢复演练。

### P1（阶段 4 必须有）

- embedding 成本控制：content hash 缓存、批量请求、限速、断点续跑、模型/维度变更策略。
- 增量入库：文件变更检测、删除检测、幂等 register、部分失败重试。
- 并发写锁与队列；避免多个 `spawn python3` 同时迁移/写 vec0。
- 观测与诊断：结构化日志、job 状态、耗时、计数、错误码、fsck。
- 备份加密/敏感路径处理，以及 raw、SQLite、manifest 的一致快照。

### P2（规模增长再做）

- 引用图层或被引次数排序（当前 researcher 工作流足够）。
- 更复杂的 reranker、跨项目权限和 Web UI。

## 最终决策建议

批准进入阶段 3，但将进入条件写入计划：完成新评分公式落地、canonical 多格式契约、A/C 召回架构决策、generation/一致性设计和量化验收集。阶段 5 只有在这些门槛通过后，才可宣布 v1.0；单次真实流程跑通不能替代质量、回归和恢复验证。
