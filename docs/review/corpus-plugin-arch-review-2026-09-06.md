# corpus-plugin 技术架构设计审查（2026-09-06）

## 总体结论：条件通过，不建议原样进入阶段 4

四支柱和已拍板方向总体一致，模块边界也基本合理；但文档目前仍有若干数据契约未闭合，尤其是 vec0/迁移、generation、评分版本共存和 worker 生命周期。建议先完成下列 P0 设计修订，再开始编码；否则在 229MB 生产库上可能出现不可回滚的数据损坏或查询读到错误代数据。

## 逐维度结论与风险

### 1. 架构自洽性：部分通过

- TS→worker→Python 核心的边界清楚，命令映射覆盖 5 个工具，spawn 是合理降级。
- IPC 仍不足以直接实现：未定义 `schema_version`、单行最大尺寸、连接复用/半包处理、未知命令、重复 request_id 的响应缓存范围、写请求超时后“已提交但响应丢失”的判定，以及 worker 重启后的幂等窗口。
- “写操作按 request_id 去重”需要持久化幂等表或 job_id；仅内存去重无法覆盖进程重启。建议协议明确 request_id 全局唯一、响应缓存 TTL、score 使用业务幂等键，并规定超时后查询状态而不是盲重试。
- 空闲 300 秒退出与“插件自动拉起/回切”存在竞态；需锁文件或单实例机制，socket 路径应安全创建、权限限制、启动 stale-socket 清理。
- 文档称 worker 不含业务逻辑，但命令分发、读写互斥、事务和健康检查本身需要明确服务层职责；当前“单一事务提交”没有定义 embedding API 在事务外还是事务内（应在事务外生成，短事务内写入）。

### 2. canonical schema：不通过（P0）

示例 nodes 只有 kind/level/path/text 等字段，不能稳定承载现有 tree-aware chunker 的 `doc_id`、parent/child/sibling、heading_chain、section_number/title、ordinal、char/source locator，也未定义列表、脚注、图片/alt、表格结构和节点 ID。仅有 path 还不足以在同级重复标题、拆分 paragraph 时生成稳定且可追溯 chunk。

必须规定：节点 ID 与文档 content hash 的关系、规范化 path 算法、节点顺序/父子不变量、原文 byte/char/page locator、表格/公式的 canonical 文本化规则、解析警告等级。`.canon.json` 与 raw“同目录”会让 PDF/DOCX/HTML/JATS 的 raw 扩展名和命名不一致，且 legacy `.md/.txt` 可能没有同目录约定；应明确 raw_root 下的相对路径、唯一命名和原始文件不改写策略。chunker 需保留 legacy md/txt 适配器，而不是声称“只吃 canonical”却未定义转换入口。

### 3. generation 状态机：部分通过（P0）

方案的 `<pmid>__<gen>__<path>__<part>` 与现有格式 `pmid__path__p<suffix>` 可区分，但没有处理 pmid/path 中 `__`、斜杠、超长 ID，也没有给出旧 ID 解析与稳定编码规则。PRODUCT 还要求 content hash+chunker 版本，ARCHITECTURE 的实际前缀只含数字 generation，二者不一致。

`documents.generation` 是活动代，但缺少 generation manifest（状态、hash、版本、创建时间、chunk 计数）。原子事务应明确只切换指针和 manifest；旧代清理必须延迟到可配置回滚窗口结束，并规定失败时新代全量删除。relevance 的 stale 应按 generation/embedding/query 策略关联，不能只有一个 stale 标志。

### 4. 数据层迁移：不通过（P0）

- `ALTER TABLE ... ADD COLUMN` 没有给出幂等实现；SQLite 不支持 `ADD COLUMN IF NOT EXISTS`，必须通过 `PRAGMA table_info`/schema_version 迁移表逐列检查。
- vec0 虚拟表不能用普通 ALTER 增加 generation 列；文档当前采用 chunk_id 前缀是可行方向，但必须明确查询只按 active generation 过滤，并提供旧向量重建/校验脚本。现有 `db.py` 仍对 vec0 使用 `INSERT OR REPLACE`，与方案“DELETE + INSERT”直接矛盾。
- 229MB 库迁移虽主要是加列，但 WAL、磁盘余量、备份一致性、锁等待和中断恢复未定义。不能只 `cp` 作为在线备份：应先 checkpoint/只读快照或 SQLite backup API，校验 integrity_check、schema 版本和 vec0 行数；迁移前检查至少 2× 余量并设 busy_timeout/维护窗口。
- 方案要求开启 foreign_keys，但现有连接初始化和旧表树字段/索引兼容未给出验证；启动自检失败时必须拒绝写入而非静默降级。

### 5. 评分系统：不通过（P0）

`quality_evidence.pmid` 主键意味着同一文献无法 append v1/v2 两个版本；“旧 v1 保留、append 新版本或原位更新”二选一未拍板。应引入 `(pmid, formula_version, rubric_version, scored_at/version_id)` 历史表或明确不可变版本表，并定义当前生效版本指针。

`recompute-scores --formula v2` 的范围、幂等性、是否只处理 evidence 行、无 evidence 是否创建 prior_only 行、失败重试和审计记录均未定义。`partial` 在架构中出现但 ALTER 默认只列 evidence/prior_only；必须统一枚举和 API 输出。

阈值 0.55/0.75 仍写成不变，但 v2 会改变分布；应明确 prior_only/partial 默认不能进入 primary 的实现位置（查询过滤还是 recommendation 策略），以及人工 override 的存储和审计。否则“阈值不变”与产品目标“高质量优先”不可验证。

### 6. MIGRATION.md：不通过（P0）

Phase 顺序把“安装 sqlite-vec”置于配置和迁移前，且安装动作与“不要装插件”约束虽不冲突，却未说明离线/系统 Python 风险。Phase 3 先 fsck 再 ALTER 没有定义基线如何保存、失败如何恢复；Phase 5 以杀 worker 验证 fallback，但没有请求中途断开、重复 score、socket stale 的验收。

回滚表述“恢复 bak + config 指回旧路径”并非真正可回滚：已写入的新 schema/数据、外部 raw/canon、worker 进程和代码版本可能不匹配；恢复期间旧 CLI 可能读取已修改数据库。需要按阶段定义停止写入、恢复数据库快照、删除/保留新代、验证 integrity/fsck、恢复服务的顺序，并明确备份保留期与演练记录。`grep=0`、5 工具全绿、评分抽查等验收缺少样本、超时、并发和失败注入标准。

## P0/P1 清单（不超过 5 条）

1. **P0**：补齐 schema 迁移框架、vec0 重建/DELETE+INSERT 语义、在线一致快照与 229MB 回滚 runbook。
2. **P0**：冻结 canonical schema 与 legacy md/txt 适配，定义稳定 node/path/locator/tree 不变量和 golden fixtures。
3. **P0**：冻结 generation ID/manifest、原子切换事务、旧代保留/回滚窗口及 stale 传播查询条件。
4. **P0**：改为可共存的评分版本模型，明确 recompute、prior_only/partial/override 与推荐阈值策略。
5. **P1**：完成 IPC 状态机（schema、幂等持久化、超时重试、连接/锁/权限）及 worker 故障注入验收。

## 阶段 4 编码前必须修正的设计点

- 在 ARCHITECTURE 中加入可执行 SQL migration contract：schema_version、逐列幂等检查、vec0 维度/距离验证、失败恢复点。
- 将 chunk ID 规范统一为可编码/可解析的格式（建议 hash+generation+path+part，字段转义），并建立 generation manifest 表；查询强制 active generation。
- 给 canonical 节点补齐 `node_id/ordinal/parent/path/locator` 和所有 chunker 必需字段，说明 raw/canon 相对路径与 legacy 转换。
- 选择不可变评分历史表及 current pointer，写出 v1→v2 回算、部分证据和人工 override 的精确定义；把 recommendation 规则从“阈值不变”改为可配置、可审计策略。
- 为 worker 定义 schema_version、request lifecycle、持久化幂等、超时后查询状态、socket 权限/锁和重启行为；MIGRATION 增加断点恢复与真正可回滚的演练步骤。

在上述修订和设计级验收标准落地前，阶段 4 只能做不触碰生产库的骨架/fixture 工作，不应执行生产迁移或切换。
