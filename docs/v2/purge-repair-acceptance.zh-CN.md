# 永久删除修复验收记录（2026-11-11，未部署候选）

## 范围与结论

先读 `PURGE_REPAIR_20261111.md`，再读 `EXECUTION_BOUNDARY.md`；核对产品方案第 10 节、
技术方案第 8/10 节及真实实现。保留六个 public tools，沿用当前执行 provider，未新增
模型/部署配置。没有访问生产、读取凭据、运行真实付费服务、deploy/restart、commit 或 push。
所有测试、HOME、TMP、数据库、包及证据在 `/opt/data/corpus-build-20261110T170301Z` 内。

**不是 PURGE_UNAVAILABLE 占位实现**：合格的原生 recycled work 经精确提案、已有独立
operator 批准记录和 apply 后，实际删除原件文件、所有版本/历史 generation 的 canonical、
chunk text/locator、FTS、向量及相关终态 job 内容，保留不含正文的身份墓碑和操作 journal。
现有 rerank 行为未改写。旧的 rerank/2026-11-10 验收报告是历史快照，其 purge 未实现结论
由本报告在以下限定范围内替代。

## 提案、依赖和稳定引用

- 入口仍为 `corpus_manage.propose(maintenance='purge')` / `apply`；需 exact target、
  expected revision、明确永久删除 reason。active/tombstone 不可 purge；合并字段不允许用于 purge。
- frozen payload 明示物理删除、未知外部引用不可穷尽、应用级清理非安全擦除；只保存正文的
  hash/fingerprint，不把 canonical/chunk 正文复制到删除提案或 journal。
- 提案与 apply 均检查依赖。apply 在 `BEGIN IMMEDIATE` 内重新生成 inventory，核对 work、
  source_versions、全部 generations/chunks、active generation 指针、assets、blob hash/size/inode、
  jobs、context links、owned identifiers。变化须重新提案，不能复用旧批准偷偷扩大删除范围。
- 阻断：任意 research revision（含草稿、历史版本）的 evidence/正文/structured 信息、citation
  （含 chunk-only）、review 的 target/source、external wiki/fact links、其他版本/标识的 provenance，
  合并别名/被合并身份、shared blobs、活动 job/租约、无法证明归属的 job、缺失 original asset 的不完整归属清单。
  采用保守 ID 检测，可能误阻断，不通过模糊推断绕过安全门。
- 不必删除传递链才能识别阻断：报告 A 的任意历史 evidence/citation 若指向来源，已阻断；
  B 引 A 的链因此同样不能让来源被删。测试含传递 report 和独立 chunk-only citation。
- 普通 context organization 与本对象 external identifiers 不是证据引用；在冻结影响清单中披露，
  删除时移除；context 对象本身保留。任何 evidence/review/report/wiki/fact 记录都不级联删除。
- shared bytes 采用更保守策略：只要有幸存 version 使用同一 hash，整个 purge 被阻断，
  连目标关联也不动。被删除 work 内部多个版本/角色共同使用的 blob 只删除一次。
- IDs/work/source_versions/generations/chunks 的非正文骨架、内容 hash、时间与操作审计保留，
  防止旧 URI 被复用。旧 work/version/chunk 读取返回显式 `purged` 或 `cleanup_pending` 墓碑，
  `content_available=false`，不是空正文冒充有效证据。不可恢复/写元数据/重新关联或用于新 evidence。
  被已知稳定引用使用的正文根本不允许删除，符合固定引用保证；未知外部旧 URI 只能解释删除，
  不承诺删除后还能返回正文。原有 merge/recycle 的正文保留语义不变。

## 文件与事务协议

1. 提案阶段只检查；apply 事务内再检查 recycled/revision/freshness/approval。
2. 同一事务写入 `purge_journal`（owned manifest、operation ID、target、pending）、清理 DB
   正文/索引、立墓碑、消费批准并写 `purge_intent` 与 pending receipt。**提交前不移动或 unlink 文件**。
   immutable 表的必要正文 redaction 在该事务内短暂替换对应 trigger，随后恢复；失败随事务回滚。
   audit/research/citation/history 的 immutable 保护不放开。
3. intent 提交后，清理事务持有 DB writer lock；基于显式 sandbox anchor，逐级
   `O_DIRECTORY|O_NOFOLLOW` 打开目录，只接受 `objects/sha256/xx/hash` 精确路径，拒绝
   traversal/absolute/custom paths、symlink、非 regular file、额外 hardlink、hash/size/inode 不符。
4. 文件先 rename 到 data root 下私有 `0700 .purge`（名称绑定 operation/hash），fsync 两目录，
   重新验 hash/inode 后 unlink，再 fsync。中断后按固定 staging 名恢复；未知替换文件不盲删。
   检查源路径无重现才允许完成。symlink swap/racing replacement 只产生 pending，不跟随到外部字节。
5. 全部 cleanup 完成才提交 `purged` receipt、journal 和唯一 `purge_complete` 审计。
   OSError/安全错误明确返回 pending 与不含内容的 error code。中断不会把未完成当成功。
   不可逆文件删除发生在 durable intent 之后；因此不是跨 DB/FS 的神奇 ACID 回滚，
   而是有意图记录、可重试的前向恢复协议。
6. 重发相同 apply（proposal/approval/domain/operation ID 必须相同）继续恢复，重启或批准到期后
   也可完成已授权 intent；不同 payload 拒绝。相同 operation 并发收敛到同一 receipt；不同 operation
   竞争同一批准仅一方可消费。ingest 的 publish 现在在 writer transaction 内，并拒绝 pending hash，
   防止先 publish/后注册与删除竞争产生 DB 指向缺失文件。不同源继续正常保留与检索。

文件 containment 假设 sandbox anchor/SQLite 与私有 staging 的所有者可信；不能抵抗具有同一
UID 任意写权限或内核权限的恶意进程无限并发篡改私有 staging/数据库。普通应用 writer 经 SQLite
串行，路径组件用 pinned dirfd，已测试的替换和链接竞态 fail closed。没有生产防攻击演练声明。

## 备份与剩余限制

- 仅应用级删除，不是 secure erasure。SQLite 空闲页、WAL、回滚/OS 缓存、磁盘快照、SSD、
  文件系统备份等可能仍有历史字节；未做 VACUUM/全盘覆写/加密销钥，也不许声称物理不可恢复。
- 有 migrations 或 legacy_map 的整个 catalogue 保守阻断 purge；不破坏 immutable raw_payload、
  legacy vectors/映射和备份审计。metadata_correction 中涉及该目标的不可变历史也阻断。
  这些限制意味着导入旧库不是“原生清理已完成”的同义词，需要另行批准的备份/历史处置方案。
- 已存在的独立 immutable backup bytes 不擅自删除。成功回执明确
  `historical_backups=not_erased_or_claimed; legacy_backed_candidates_blocked`。
  用户手工复制、无注册的同文副本、外部引用/备份不能穷举；不承诺 corpus 外的副本已清理。
- 只清理本实现已知的 managed original 和 DB 派生内容；不运行 general orphan GC，不擅自删 temp、
  wiki/fact 或 research 正文，不自动后台恢复 pending。operator 必须保留原 apply 请求并重发。
- 保留 receipt、proposal、manifest、hash、ID、时间等非正文审计；不是删除所有元信息。
- operator CLI 的 `identity_verified=false` 保持真实。测试中批准是明确 synthetic operator 操作，
  不能当 genuine human approval。真实宿主绑定/人类身份审批仍是上线前独立门槛。

## RED / GREEN 与执行证据

证据目录：`evidence/purge-repair-20261111/`。只收集 `tests/safe`，旧测试不自动收集；
pytest 的 sandbox-root、basetemp、HOME/TMPDIR 显式隔离，socket/DNS 默认阻断。

| 阶段 | 实际结果 | 证据 |
|---|---|---|
| 首次安全 purge/active 阻断 RED | 2 failed，exit 1 | red-initial.log |
| 依赖/containment/recovery/concurrency RED | 22 failed, 4 passed，exit 1 | red-safety-recovery.log |
| 首轮实现验证（保留失败，不冒充 GREEN） | 12 failed, 14 passed，exit 1；Landlock 拒绝打开 `/`，改由授权 anchor 开始 | green-1.log |
| 第一轮 GREEN | 26 passed，exit 0 | green-2.log |
| 首次全安全套件 | 197 passed，exit 0 | full-initial.log |
| 墓碑新 evidence / cleanup replacement RED | 3 failed, 34 passed，exit 1 | red-races.log |
| 对应 GREEN | 37 passed，exit 0 | green-races.log |
| ownership/fresh active pointer/smoke helper RED | 6 failed, 38 passed，exit 1 | red-final-guards-smoke.log |
| 对应 GREEN | 44 passed，exit 0 | green-final-guards-smoke.log |
| 墓碑 metadata/link 写回 RED | 2 failed, 47 passed，exit 1 | red-tombstone-writes.log |
| 墓碑写保护 focused GREEN | 49 passed，exit 0 | green-focused.log |
| 首次明确安全收集检查 | 220 tests collected，exit 0 | collection.log |
| 首次完整 source/sdist/wheel | 各 220 passed，compile/package/probe 均 exit 0 | validation-first/ |
| 缺失 original ownership inventory RED | 1 failed, 45 deselected，exit 1 | red-ownership.log |
| 最终 focused GREEN | 50 passed，exit 0（purge 46 + synthetic smoke 4） | green-ownership.log |
| 最终明确安全收集检查 | 221 tests collected，exit 0 | collection-final.log |

并非每个新增覆盖都故意改坏已有正确行为；已有正确 freshness/containment 等附加覆盖作为验证，
不冒充先失败再修复。每次实际修复前的失败输出及中间失败均保留。

完整验证使用规定命令：

```bash
tools/validate.sh --sandbox-root /opt/data/corpus-build-20261110T170301Z --python /opt/data/corpus-build-20261110T170301Z/validation-venv/bin/python
```

完整结果与环境、source/sdist/wheel import 路径、compile、schema/contracts/probe、artifact hashes
在同证据目录的 `validation/` 与 `validation-console.log`，不把静态 compile 计为业务用例。
最终三个套件各 **221 passed**；三个 compile 及 package/probe 均 exit 0。

## 父进程真实服务 smoke（worker 未执行）

`corpus_core/live_smoke.py` 为 credential-free、显式 opt-in 工具，不新增 public tool。
父进程准备 boundary 内非秘密 settings，data_root 必须不存在；现有 key 只通过指定 env 名继承，
不 echo、不序列化，不读取 .env。不向工具传 key 值。

settings 使用现有严格格式；embedding profile 为 `provider=dashscope`、
`exact_model=text-embedding-v4`、`dim=1024`；policy `operation_ids=['live-smoke']`、
`max_calls=1`、`batch_size=1`、`max_chars<=2048`、显式正数费用上限。
rerank 为 `protocol=dashscope_native`、`exact_model=qwen3-rerank`、
`candidate_budget=max_documents=3`、`max_calls=1`、`max_chars<=2048`、显式 timeout 与单文档上限。
endpoint 仅允许 DashScope 国内/国际 HTTPS 标准 embedding/native rerank 路径。

```bash
python -m corpus_core.live_smoke --sandbox-root "$AUTHORIZED_ROOT" --settings "$NONSECRET_SMOKE_SETTINGS" --execute-paid-smoke
```

父进程须单独授权费用，确保非 offline、隔离 HOME/TMP、data_root 全新，不使用生产 DB。
只发一条固定短文 embedding 与三条固定短文 native rerank；无自动 retry/批量重嵌。
直接通过 candidate EmbeddingRuntime.batch/APIEmbedding 与 RerankRuntime.rank/normalize，
不会用旁路 HTTP 伪造 adapter 成功。fallback/ambiguous 不算成功；utility 输出仅维度/状态/文档数。
失败后不复用该 root 重发；需先由父进程判断 vendor 的计费/真实响应。

worker **没有执行这个 live utility**。4 个离线测试只替换 runtime transport，检查真实 adapter
payload/normalization 和预算门；不证明真实供应商成功、provider 模型身份、费用或人类批准。
父进程真实 DashScope 结果须单列，不包含在这 221 个 synthetic/offline 业务测试中。

## 实际修改路径

- `corpus_core/purge.py`、`corpus_core/live_smoke.py`（新增）。
- `corpus_core/db.py`、`corpus_core/service.py`、`corpus_core/research.py`、
  `corpus_core/management.py`、`corpus_core/contracts.py`、`corpus_core/probes.py`。
- `tests/safe/test_purge_boundary.py`（将拒绝审计替换为实际删除验收）、`tests/safe/test_live_smoke.py`（新增）。
- `tools/validate.sh`、`README.md`、`docs/ACTION_CONTRACTS.md`、`docs/REQUIREMENTS_TESTS.md`、本报告。
- `evidence/purge-repair-20261111/`（RED/GREEN/完整验证和修改路径清单）。
