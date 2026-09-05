# corpus-plugin 架构复审报告（2026-09-06）

## 总体结论

**不通过，暂不放行阶段 4 的生产实现/迁移。** v0.2 已把上轮四个 P0 的主要概念补进 ARCHITECTURE，但其中至少三项仍未形成可直接编码、迁移和回滚的闭合契约；且现有源码与新契约仍有明确冲突。可以开始不接触生产库的 fixture、接口骨架和迁移测试，但不能切换生产 generation、执行正式评分迁移或声称阶段 4 已放行。

## 逐维度对比表

| 维度 | 上轮意见 | 本轮验证 | 结论 |
|---|---|---|---|
| 1. canonical schema | 缺 chunker 字段、树不变量、locator、legacy 适配；raw/canon 布局不清 | §5.2 补了 node_id/ordinal/parent/path/locator、警告和 md/txt 适配器，raw/canon 分目录也清楚；但 `kind=table/formula/figure` 使用 `markdown/latex/alt` 而必需字段表要求 `text`，未规定 canonicalizer 如何填充统一 text。`node_id=n<ordinal>` 不是内容稳定 ID；ordinal/路径随解析变化会造成 chunk 漂移。更关键是当前 `process-raw` 仍直接读取 `documents.raw_path`、同名 `.md`，按后缀运行旧 chunker 并先删除全部 chunks，完全没有读取 `.canon.json` 的实现路径。 | **未闭合 P0** |
| 2. generation 状态机 | 缺 manifest、稳定编码、hash+chunker 版本对齐、原子切换/回滚窗口/stale 传播 | manifest、building/active/retired/failed、7 天窗口和 relevance stale 规则已写明；content_hash 进入 documents/manifest。但 manifest 没有数据库约束保证每个 pmid 只有一个 active，documents.generation 也不是实际 FK；chunk_id 转义把 `/` 替换成 `_`，与原有下划线可能碰撞，且“hash+chunker 版本”仍未进入 ID（仅存 metadata）。旧 ID 查询/向量过滤的兼容边界未定义；现有 process-raw 是破坏式清空而非新代构建。 | **部分闭合，仍 P0** |
| 3. 数据层迁移 | 逐列幂等、vec0 不 ALTER、backup API、启动拒写、229MB 风险、v3 重建未落地 | §6.3 给出 user_version、table_info、磁盘/WAL/integrity/fsck、backup API 和拒写原则，方向正确；但 MIGRATION 的 Python 示例在单引号 heredoc 中写入字面量 `$(date...)`，不是有效日期命名，后续校验也用占位 `...` 路径。未给出原子执行/中断恢复的具体脚本或锁定写入步骤。`quality_evidence_v3` 重建没有处理旧表当前实际主键/列差异、外键、去重和失败恢复点；“current 指针”没有表或唯一索引实现。 | **未闭合 P0** |
| 4. 评分系统 | v1/v2 不能共存；recompute、prior_only/partial/override、推荐策略未定 | v3 append-only、formula/rubric 字段、状态枚举和策略层方向正确；但 schema 没有 `override_reason/override_by`，也没有 current 指针表/唯一性约束，按 `scored_at DESC` 遇并列不确定。ARCHITECTURE 要求按 `(pmid, project, formula, rubric)` 判重，与 append-only 历史及重新评分语义冲突；`--create-prior-only` 默认不生成又与“无评分 fallback”输出契约未接通。现有 `db.py` 仍是 `max(prior,evidence)`、原位 `INSERT OR REPLACE`，query/score 仍读旧表，make_recommendation 仍硬编码阈值。 | **未闭合 P0** |
| 5. IPC | 缺 schema/帧/超时/持久幂等/单实例/权限 | schema_version、1MB、幂等表、score-status、pidfile/flock、权限和命令集已补齐。仍有契约矛盾：幂等表只有 `result_sha`，没有缓存响应正文，重复 query 无法按文档“直接返回缓存结果”；score 业务键含 `scored_at`，重试若重新取时间会产生两次写入；worker 失败后自动 spawn 与“单配置源/旧路径兼容期”的切换优先级未写成状态机。 | **部分闭合** |
| 6. MIGRATION | 顺序、真回滚、失败注入不足 | Phase 0→环境→配置→迁移→评分→worker 顺序和快照恢复原则已补；失败注入也列出断连、重复 score、stale socket、磁盘满。可执行性仍不足：Phase 1 先 `pip install` 依赖且没有离线/版本锁定/失败回滚；Phase 3 fsck 基线文件写入仓库路径但未定义生产权限/保存位置；恢复快照时未明确停止所有旧/新 writer、清理 WAL/SHM、重开连接和验证代码/schema 兼容。 | **条件不足** |
| 7. 新矛盾 | 需扫描 v0.1→v0.2 与产品/源码冲突 | 发现多处：PRODUCT/PLAN 仍描述旧 max 公式、旧阶段状态；ARCHITECTURE 宣称工具参数不变但 worker 的 `search_self`/`similar_level` 命名及 JSON 输出尚无兼容适配；`quality_evidence_v3` 与现有 `corpus score` 文本输出、query 读取旧表不兼容；`ingestion_jobs.generation` 与 manifest 双写没有“谁是真源/一致性校验”规则；vec0 查询只写 LIKE active generation，但现有 `vector_search` 没有 generation 过滤；db.py 两个 insert 方法仍 `INSERT OR REPLACE`。 | **存在阻断矛盾** |

## 新矛盾清单

1. **canonical 入口冲突（P0）**：文档规定 `.canon.json → 唯一 chunker`，源码 `cmd_process_raw` 仍读 raw/Markdown/TXT，且先物理删除旧代；直接按文档实施会改变现有 process-raw 行为并破坏回滚。
2. **稳定 ID 契约未满足产品要求（P0）**：产品要求 content hash + normalized path + chunker version；新格式只有数字 generation/path/part，hash 与 chunker version 不在 ID。转义规则存在碰撞，旧 ID 与新 ID 的唯一解析/查询策略缺失。
3. **评分 current/输出契约断裂（P0）**：v3 只定义历史表，current 是注释中的“最新 scored_at”；没有确定性 current 指针。旧 `score` 仍更新旧表并输出文本，query 仍查旧表，无法实现 v1/v2 共存和 `prior_only/partial/override`。
4. **vec0 active 过滤未覆盖现有路径（P0）**：架构要求所有查询按 active generation 过滤，`CorpusDB.vector_search()` 直接对 vec0 KNN，无 generation 条件；仅靠 chunk_id 前缀无法防止旧代进入候选。
5. **幂等语义自相矛盾（P1）**：持久化表声明缓存响应，却只存摘要；score 的业务键把时间戳作为键，不能保证超时重试幂等。
6. **迁移示例不可执行（P1）**：快照文件名日期不会展开，恢复命令含省略号；这会让 229MB 生产迁移的首个安全门失效。

## 阶段 4 放行条件

在允许生产实现前，至少必须补齐并由测试证明：

1. 冻结 canonical JSON 的字段语义（所有 kind 的 text/结构字段、稳定 node_id、locator）并实现 `process-raw` 的 canon-only 入口；保留 legacy adapter 的 golden fixtures。
2. 冻结可解析且无碰撞的 chunk ID（明确 hash/chunker version 是否编码或由 manifest 唯一绑定），建立 manifest 的 active 唯一约束、原子切换/失败清理/旧代恢复测试；所有 vec0 KNN 路径强制 active generation。
3. 实现可审计的 v3 迁移：旧表列映射、current 指针表、override 审计字段、确定性 recompute 幂等键；同步改造 `db.py`、`cli.py`、worker 和 TS 输出契约，保证 v2 公式和 status 在 query/score 全链路可见。
4. 提供可执行的 backup/restore/runbook（修正示例，停止 writer、WAL 处理、完整路径、失败恢复点），并在复制规模接近 229MB 的 fixture 上做中断、重跑、磁盘不足和 integrity/fsck 验收。
5. IPC 幂等表要能返回完整响应，定义固定业务 idempotency key 与 worker/spawn 状态转换；完成半包、超时已提交、重复 score、stale socket 的自动化测试。

在上述条件完成前，阶段 4 仅建议进行不触碰生产库的骨架、契约测试和 fixture 工作；最终结论为 **不通过**。
