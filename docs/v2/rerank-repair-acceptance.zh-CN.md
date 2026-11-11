# Rerank 修复验收记录（2026-11-11，候选目录限定）

## 结论与边界

本次针对实际 baseline 新增 rerank，不假定不存在的 r2 交付。仅修改当前候选仓库；
没有生产读取、真实服务调用、部署、提交、push、重嵌入、凭据查看或安装配置变更。
离线 synthetic fixtures **不等于真实 DashScope/OpenAI-compatible 服务验证**，
不声明生产可用。旧验收文档中“reranker 未实现”的历史结论由本报告更新；其他限制不变。

保留原六工具：`corpus_ingest`、`corpus_search`、`corpus_read`、
`corpus_reference`、`corpus_manage`、`corpus_jobs`。contracts、plugin 注册和审批接口未改。

## 精确变更文件

- `corpus_core/rerank.py`：严格可选配置、两种显式协议、规范化、持久预算与短期排序快照。
- `corpus_core/settings.py`：只增加可选顶层 `rerank`，兼容原 embedding 配置及 null。
- `corpus_core/service.py`：构建可选 runtime；注入 fixture 仅限显式 test mode。
- `corpus_core/retrieval.py`：实际 hybrid/RRF 之后接入；限制发送候选；分页前验证 limit/binding。
- `corpus_core/paging.py`：提取并复用 cursor 签名、binding、过期校验，保留 snapshot 检查。
- `corpus_core/embeddings.py`：JSONAPITransport 增加可选 socket timeout；旧调用默认行为不变。
- `corpus_core/network.py`：现有 DNS 固定、公网 HTTPS 和响应限额请求函数支持显式 timeout。
- `tests/safe/test_rerank.py`：48 个离线测试实例，所有协议响应均标记 synthetic。
- `tests/safe/test_purge_boundary.py`：1 个现有 purge 拒绝行为审计回归测试。
- `README.md`：实际配置、硬限额、警告、快照语义与限制。
- `docs/rerank-repair-acceptance.zh-CN.md`：本报告。
- `evidence/rerank-repair/`：全部 RED/GREEN、完整验证与编译记录、命令索引。

`tools/validate.sh`、contracts、数据库既有 schema version、embedding checkpoint 算法未改。
仅启用 rerank 时创建 `rerank_budgets` 和 `rerank_snapshots` 两张附加表；未配置不建表。

## 实际行为与限额

配置必须显式包含 `protocol/endpoint/exact_model/key_env/timeout_seconds/`
`candidate_budget/max_documents/max_document_chars/max_calls/max_chars`，不接受未知字段。
协议只有 `dashscope_native` 或 `openai_compatible`。只用指定 env 查 key，不猜 key、host、model，
不自动补 endpoint、不跟随 redirect，不重试。baseline 没有多 endpoint 机制，本次也未扩展。

DashScope POST：`model` 使用 exact_model（示例 `qwen3-rerank`），
`input={query,documents}`、`parameters={top_n,return_documents:false}`；读取 `output.results`。
兼容协议 POST `model/query/documents/top_n`，读取 `results`。
两者都要求返回所有提交候选、唯一整数 index（拒绝 bool/越界/重复）、有限数值 relevance_score。
响应文档字段不会覆盖本地 chunk。不同 model 身份、非法 JSON/shape、NaN、数值溢出均不冒充成功。

硬上限：candidate_budget、max_documents 各 1..100；每文档 Unicode 字符 1..100000；
累计调用 1..10000；累计 query+documents Unicode 字符 1..10000000；
socket timeout >0 且 ≤60 秒；响应 ≤1 MiB。DNS 使用既有 OS resolver，timeout 不是包含 DNS 的总墙钟期限。
累计预算按候选数据库 + 精确配置指纹持久化，调用前原子预留；失败也保守计数；不是全账户金额上限。
超长文档不暗中截断，直接保留完整的确定性 pre-rerank 结果并报 degraded。

scope、filters、活动 generation/lifecycle 都在发送前执行；仅发送满足限制的候选前缀。
formal/preferred 优先级、每版本最多五 chunk、固定 URI、locator、source status、review/撤稿警告不变。
未发送的尾部维持旧顺序并给出 `rerank_candidate_limit`。成功明确 `rerank_complete`；
异常、缺 key、离线、服务失败、响应错误或预算耗尽明确 `rerank_degraded_*`，不伪报成功。

快照仅存 scores/status，不存供应商返回正文；绑定配置/查询/scope/filters/候选证据，15 分钟后清理。
顺序翻页重用快照不重发 rerank；更改 binding 的 cursor 在 rerank 前拒绝。
并发或崩溃中的 in-flight 预留返回旧排序 + 警告；完成后不同 snapshot 的旧 cursor 拒绝为 stale，
不把不同顺序混页。缺 key/失败也缓存 15 分钟，因此修复环境后同查询不会立即重试。
embedding 成功 checkpoint、模糊已计费调用不盲重试语义保持原样，全套已有测试仍通过。

## RED / GREEN 原始证据

命令、环境、各轮精确 basetemp/cache、真实退出码见 `evidence/rerank-repair/commands.md`。
所有 HOME/TMP、数据库和结果在 `/opt/data/corpus-build-20261110T170301Z` 内。
测试 fixture 禁止 `getaddrinfo/create_connection/socket.connect/connect_ex`；只收集 `tests/safe`。

| 轮次 | RED 实际结果 | GREEN 实际结果 |
| --- | --- | --- |
| 初始接口/适配器/兼容/降级 | 32 failed（缺配置入口和实现） | 32 passed |
| 边界、hybrid、状态、并发与 HTTP fixture | 6 failed / 36 passed | 42 passed |
| 数值溢出、HTTP 失败、响应限额 | 2 failed / 46 passed | 48 passed |
| 加入 purge 拒绝审计 | 无功能修复；测已有拒绝行为 | 49 passed |

第二轮的 6 项失败中，4 项是实现需要提前校验 limit/cursor，否则非法请求也可能扣预算；
另 2 项是新测试自身误把 monkeypatch 后函数保存为 original，造成递归；修正了 fixture，未伪报为产品缺陷。
第三轮发现超大整数 timeout/score 导致 OverflowError，已分别通过先范围校验和安全有限性校验修复。
`rg` 不可用（原始错误 `/bin/bash: line 1: rg: command not found`）；改用 find/grep/sed。
未出现 containment 失败；所有最终已运行测试退出码均为 0。

## 全套与编译

实际命令：

```bash
bash tools/validate.sh \
  --sandbox-root /opt/data/corpus-build-20261110T170301Z \
  --python /opt/data/corpus-build-20261110T170301Z/validation-venv/bin/python
```

第一轮（添加 purge 审计测试前）：source/sdist/wheel 各 **171 passed**；
source、sdist、wheel compileall、打包、包内导入及 capabilities 均 exit 0。
完整原始日志：`evidence/rerank-repair/full-validation.log`；
逐项原始证据：`/opt/data/corpus-build-20261110T170301Z/evidence/validation-H4XzSDB6`。
最终新增 purge 审计测试后的完整重跑：source/sdist/wheel 各 **172 passed**，
三份 compileall、打包、包内导入和 capabilities 均 **exit 0**，无最终失败。
合并日志：`evidence/rerank-repair/full-validation-final.log`；
编译日志同时复制到 `evidence/rerank-repair/source-compile.log`、
`evidence/rerank-repair/sdist-compile.log`、`evidence/rerank-repair/wheel-compile.log`。
逐项原始证据与候选构建产物：`/opt/data/corpus-build-20261110T170301Z/evidence/validation-MHVIPNzx`。

## 永久删除审计：仍是 blocker

`corpus_core/management.py` 的 `propose_maintenance` 标注 physical_delete 不可用，
`_apply_maintenance` 即使经过 proposal/revision/dependency/approval，也抛 `PURGE_UNAVAILABLE`。
本次未实现物理 purge，不把 merge 或 recycle 当作永久删除。
新增离线测试在 recycle 后创建并批准 purge proposal，重复执行仍拒绝；固定引用和所有原始 bytes 保留。
该测试只证明拒绝安全，不证明真正删除、共享对象 GC、未知外部引用处理或并发删除已实现。
安全物理删除的 recycle prerequisite、共享/依赖引用库存、审计/幂等/并发完整实现仍需另行授权设计与测试。

## 未解决与未验证

- 无真实 vendor、真实凭据、真实 SDK/human approval、生产迁移或生产就绪验证；由 parent 独立审核。
- socket timeout 不替换 DNS 总时限；预算按配置而不是账户总花费计，不能承诺货币金额上限。
- 短期缓存不是跨所有并发请求的永久不可变会话；in-flight 或证据变化可令 cursor stale，需重新开始查询。
- 保留既有 flat chunk/有界 exact vector、OCR/复杂分块等原有限制；不扩展六工具产品范围。
- 安全物理 purge **未实现，仍为 blocker**。
