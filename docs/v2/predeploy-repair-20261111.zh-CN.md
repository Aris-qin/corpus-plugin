# 2026-11-11 部署前候选修复与未闭环门禁

## 范围与结论

仅修改候选；未访问生产目录/数据库、未部署/重启、未提交/推送 Git，未读取凭据、调用付费服务、重嵌入旧库或修改 wiki/fact。所有运行文件位于 `/opt/data/corpus-build-20261110T170301Z` 下。仍只有原批准的六个模型工具；新增的是既有业务层的本地维护 CLI/证据契约，不是第七个工具。

本报告优先于旧 README/验收稿中关于 operator 批准、正式发布、永久删除可执行性的描述。当前产品采用**保守默认禁用**，没有可信真人批准集成，也没有允许开启敏感动作的配置。不能据离线测试声称可以上线或无损接管全部旧资料。

## 1. 目录安装加载

根因：插件根绝对导入 `corpus_core`，只有显式把候选加入解释器路径时才成功。根入口改为包内相对导入，插件私有模块不再抢占独立安装的顶层 `corpus_core`。wheel 同时保留独立 CLI 包，并把完整私有包放入 `hermes-plugins/corpus-candidate` 数据目录，不能只复制一个依赖全局搜索路径的入口。

完整包复跑额外发现 sdist 解包目录名含 `-0.1.0` 时 pytest 会把根入口作为无 package 的裸模块导入；纯相对导入因此失败。先保留 sdist 全套 setup 错误和 `red-bare.*`（1 失败/1 已通过），再将裸文件入口正规化为其自身私有 package（`__package__/__path__`，不修改 sys.path）。新增 fresh subprocess 的裸加载/正常 package 双场景，恢复 source/sdist/wheel 互不污染。

`test_directory_entrypoint_without_candidate_import_path` 在独立 HOME 下复制到 `hermes-home/plugins/corpus-candidate`，使用全新 `python -I` 子进程、清除 PYTHONPATH，从非候选 cwd 按包加载入口；断言六个契约、没有顶层模块污染。这是**Python 目录入口导入测试，不是 Hermes PluginManager 真实发现测试**。验证脚本另外实际建立新 venv、离线安装 wheel，验证独立包导入来源、console CLI 和 wheel 自带目录入口，不依赖源码目录。

SDK 精确来源：父级既有报告使用 `/opt/hermes` 主仓库中的 `hermes_cli/plugins.py`（`PluginContext(manifest, manager)`、真实 registry），已有证据为上级 `evidence/sdk-baseline.json` 及 `evidence/predeploy-20261111T025431Z/live-sdk-result.json`。它们不是本轮修订版本的发现证据，也未记录可据此断言的 SDK commit/hash。当前隔离 venv `python -I` 的 `find_spec('hermes_cli')` 为 None，worker 无权读 `/opt/hermes`，没有伪造 SDK 或调用该路径。

**父级门禁**：记录真实 SDK 文件路径/commit/hash，在新 subprocess 中从独立 HERMES_HOME/plugins 加载精确交付目录，运行真实 `PluginManager.discover_and_load`，确认 enabled、无警告、恰好六工具和实际 handler；不得注入候选 sys.path/PYTHONPATH。宿主 isolation 必须是 **`in_process`**，不是 `inprocess`。独立 wheel 安装和插件目录安装需各自验收。

## 2. 部分索引与安全恢复

根因：向量阶段不论返回何状态都写 succeeded，公共 retry 又拒绝该状态。现在：

| 状态 | 能力与恢复 |
|---|---|
| `succeeded` + `complete` | 已完成配置的向量索引 |
| `succeeded` + `not_configured` | 明确未请求向量；不是宣称语义索引完整 |
| `partial` + `budget_exhausted` | 零/部分向量；原件、词法读取/搜索可用；显式修改操作员预算后用既有 `corpus_jobs.retry` |
| `partial` + `scope_not_approved` / `configuration_changed` | 保留可用内容；恢复原操作范围/精确配置后显式 retry |
| `partial` + `provider_not_sent` | 已确定请求尚未发出的离线/缺密钥/鉴权或 URL 预检/DNS 失败；不计费预留转为 not_sent，修正前置条件后经既有 jobs.retry 安全恢复 |
| `partial` + `incomplete_ambiguous` | 网络超时、无效/零向量或未知已计费结果；拒绝自动 retry，先本地操作员复核 |
| `failed` / `cancelled` | 既有失败/取消语义；相同计费未知门禁仍然适用 |

checkpoint 公开 `generation_id/vector_status/embedded_chunks/total_chunks/capabilities/recovery`。既有 list schema 增加 partial。读取和搜索出现 `index_incomplete`；词法可用不再等于完整完成。partial 不替换已有可用 generation；新资料没有旧代时可启用其词法代。完成恢复后沿用原 generation/chunk ID，并原子完成激活。

解析结果持久化为 `job_parse_cache`，按 job/pipeline 绑定；每次仍先验证原件 hash/size。成功 provider batch 持久化且恢复不重复请求；失败激活、并发 retry/runner 租约沿用原幂等/事务保护。永久删除的历史模拟路径也清理解析缓存。打开候选库会把旧 checkpoint 明确不完整却 succeeded 的 jobs 修正为 partial，追加审计，不覆盖历史 job_steps。

### 公开恢复契约

1. 使用 `corpus_jobs.status/list(state=partial)` 查看 checkpoint。
2. 本地操作员在非密钥 settings 中显式调整预算/原 operation_ids；工具不能提交扩预算字段。retry 只在 failed/cancelled/partial 时绑定该策略；profile、endpoint、key 变量绑定、batch_size 不可换。没有新 binding 的历史 job 只能在原 policy 不变时恢复，否则继续门禁。
3. 调用 `corpus_jobs.retry`，使用新 operation_id；相同 ID 同输入重放原 queued 回执，不重复扩预算/排队；异输入冲突。随后运行现有 runner。
4. `embedding-recovery-inventory` 返回批次 ID、状态、计数、请求 key 和当前策略，不返回凭据。不确定批次会保守阻断同 profile 的后续新请求。
5. `reconcile-embedding --manifest FILE` 仅接受精确 `batch_id/profile_id/vectors/evidence`，校验数量、维度、非零、有限值、冲突和活动任务租约；只提交**供应商找回的已有结果**，不授权重新调用。相同结果幂等，冲突拒绝。记录 manifest hash，身份仍是 `identity_verified=false` 的本地操作员断言，不冒充真人批准。应先停下隔离 runner/查询并核对供应商请求/计费；没有可恢复结果、旧批次无请求计数或来源仍不明时保留门禁，不猜测结果、不盲目付费重试。

预算按显式 policy 记账，改策略表示本地操作员提供一个新的有界预算，并不等于验证供应商账单或宿主真人批准；不声称抵抗同 OS 权限改文件/代码。运行中的/queued job 不会因 settings 变大而自动增额。

网络失败按是否能够证明尚未发送区分：只有原生 transport 的明确 pre-send 错误可标 not_sent；普通 HTTPS 错误、超时、响应/零向量错误、HTTP 429/5xx 等仍保守 ambiguous，不因 retryable 标志就重试。`red-not-sent.*` 先复现两个确定未发送的失败被误记 ambiguous，再实现安全恢复。`red-late.*` 先复现租约过期后迟到结果覆盖操作员已找回向量；现在成功/失败迟到响应均不能覆盖已 reconciliation 的结果，冲突明确报错并保留恢复值。

## 3. 默认安全模式与敏感入口盘点

根因：旧 operator_approve 只绑定内容，没有可信身份，模型可自行签发。当前未能验证宿主 approval adapter，因此不保留产品侧模拟批准开关。严格 settings 新增可选 `safety_mode: "disabled"`，默认相同；未知值、test_simulation、旧自填开关均拒绝。`test_mode=True` 仅允许传输测试适配器，不能开启批准。

| 入口 | 当前行为 |
|---|---|
| 两个工具的 apply（正式发布、批量、合并、删除） | 写事务/receipt 重放之前返回 `SENSITIVE_ACTION_DISABLED` |
| Python/CLI `operator_approve` / `operator-approve` | 在签发或更新提案前拒绝 |
| Python `apply_proposal/apply_purge/recover_purge` | 包括旧批准、已成功 receipt、cleanup_pending，全部先拒绝 |
| 内部正式发布/批量/维护/删除实现 | 同一门禁，不依赖隐藏 schema |
| 正式报告 export outbox | 无待办时空返回；有待办时写文件/状态之前拒绝，包含旧库正式状态 |
| CLI probe 能力报告 | approval/purge 明确返回 disabled_no_trusted_host_adapter，不再宣称旧 operator 路径可执行 |
| 草稿、提案、拒绝提案、检索、读取、原件正常收录、回收/恢复 | 继续可用，不把非敏感流程误禁用 |

拒绝路径不写 approval、receipt、outbox、文件或半个 batch。测试保留旧成功协议（payload/revision 绑定、失效、依赖变化、并发发布、purge 恢复等），相关模块显式使用 `simulated_approval_boundary`：只在隔离 pytest 中 monkeypatch 门禁函数，**不是产品配置，不是真人批准**。新修复测试不使用该 fixture，覆盖真正默认行为、旧 approval/receipt、CLI、公开 Python、内部入口和旧正式导出。

信任假设：权限来自宿主/内核和数据根 ACL，而不是同进程 Python 对象。能编辑相同代码/数据库、注入 Python 或具备相同 OS 权限的进程当然能绕过应用逻辑，本实现没有声称阻止它们。正式开启敏感操作必须先另行实现、独立验证宿主 adapter，绑定可信身份、proposal、精确 payload/revision/有效期并复跑验收；当前没有这种集成。

## 4. 旧库业务接管与不可伪造的缺口

本轮只用已有隔离报告和合成 fixtures；不访问原生产目录/DB，不重新扫描报告中列出的外部路径，不迁移或重嵌真实旧库。父级既有 `migration-result.json` 记录 418 文档、42,548 chunks、42,498 vectors；15 条 quarantined/self_written/unknown、2 条孤立向量和大量层级引用仍待审核。这些是父级历史证据，不是本轮重新验收成功。

### 可执行盘点

`legacy-inventory` 输出精确 snapshot/legacy ID/稳定 target、custody/classification、所有 migration_issues、未决层级引用，以及每条 vector 的 raw_vector_hash/profile/目标 chunk。不会猜分类、补 parent/sibling 或把孤立向量匹配到同名条目。15 个未决条目仍不进入普通搜索；2 条孤立向量仍保存、无搜索目标。当前列表要求操作员逐项建立可验证分类/关系证据，未提供自动分类/批量放行入口。

### 原件 dry-run / 只读复制接管

`adopt-original --manifest FILE` 默认为 dry-run，`--apply` 才执行。严格 manifest：

```json
{
  "source_hash": "exact-isolated-snapshot-hash",
  "legacy_id": "exact-legacy-document-id",
  "path": "/ABSOLUTE_AUTHORIZED_SANDBOX/original.txt",
  "expected_sha256": "64-lowercase-hex-characters",
  "expected_size": 123,
  "identity_evidence": {"kind": "preserved_original_sha256"}
}
```

先绑定 exact legacy_map/raw_payload/目标，验证授权路径、存在性、hash/size 和历史身份依据。支持旧不可变 payload.extra 的 `original_sha256`，或由可信本地集成注入 `legacy_verifier('original_identity', binding)` 验证独立人工映射证据。没有原哈希且没有可信 verifier 时，**即便新算 hash 正确、文件名相同，也只返回 external_unverified 门禁**。缺失显示 missing，同名异内容显示 content_mismatch；不扫描目录、不自动任选候选。

apply 将验证过的字节只读复制为内容寻址对象，链接到已有 legacy version 的 `legacy_original` asset，记录 manifest hash/custody/revision/audit；不改动外部原件、旧 snapshot、canonical 或切片。事务失败回滚业务状态并清理本次新建无引用对象；成功重放不复制、不重复关联，原件后来缺失时仍可验证托管副本返回幂等回执；变更映射须复核而非覆写原采用记录。

真实库存没有原 hash 的条目仍需要父级独立身份核验与有权限的可信 verifier。已找到同名路径不算身份依据。本轮未伪造该 verifier 或宣称 266 个候选均为原件。

### 向量 profile 有证据认领

`claim-legacy-profile --manifest FILE` 默认为 dry-run；manifest 精确包含 `source_hash/profile/vectors/evidence`。vectors 是 `{legacy_chunk_id: inventory.raw_vector_hash}`，只覆盖经独立证据证明来源的子集。完整 profile 包括 provider、exact_model、dim、metric、preprocessing_version、provenance；改变维度或随便填模型没有任何兼容性效力。

必须由可信集成代码显式构造 `Corpus(..., legacy_verifier=...)`；verifier 收到完整 manifest、profile_id、精确目标 chunks，独立验证逐条来源/供应商或可靠生成记录（不能把调用方同权限写入的 evidence 字段本身当证明）。没有 verifier 的 CLI 只生成阻断报告，apply 拒绝。任意猜测、mock provider、孤立向量、hash/维度错误及冲突 profile 全部拒绝。生产并未接入 verifier；测试回调明确是合成夹具，不是真实证明。

部署权限要求：verifier 实现及其独立证据必须属于模型无写权限的宿主控制域，由可信启动器注入；做不到就不注入并继续隔离。普通模型工具、settings、CLI manifest 都不能注册 verifier。本轮没有验证这种部署权限，不把同权限 Python 提交的证据叫作可信宿主证明。

通过后只复制所选已存在向量值至精确 profile 的原生索引，并保存原 raw_vector 不变；未知/未选条目继续 legacy_unknown，搜索显示隔离警告。认领、回放、晚期故障回滚有测试，不触发网络或 reembedding。既有批量 `index_vectors` API 也拒绝迁移 chunks，避免绕过 provenance 门禁。**不能宣称全部旧向量可用**；真实来源可能混有 mock，必须逐子集验证，无法验证者继续隔离。

## 5. RED/GREEN、完整安全验证与命令

先审阅 tests/safe 的 conftest/import-time：仅收集该目录，导入本形态候选并核对路径；隔离 HOME/TMPDIR；所有网络普通测试禁用；没有收集历史 unsafe *_test.py。conftest 现在还强制调用者显式设置 CORPUS_SANDBOX_ROOT/CORPUS_DATA_ROOT。

证据目录为上级 `evidence/repair-20261111/`：

- `red.log/red.xml/red.exit`：修实现之前 7 个原始失败；原输出保留。其中 purge 夹具随后修正了 recycled/revision 前置条件，不将该夹具错误算作安全门禁复现。
- `red-baseline*.log/xml/exit`：从父级已交付 sdist 独立解包、覆盖新回归测试后重放 RED；导入位置与原 artifact SHA256 单独保存。这是针对保留旧实现的补充 RED，不冒充所有新增场景都在首次编码前运行。
- `red-bulk.*`：先复现旧库 bulk API 可以调用重嵌 provider，再加禁止旧库重嵌的实现。
- `green1.*` 保留中间失败；`focused2.*` 发现向量认领重放多写审计的失败，修复后 `focused3.*` 26 项通过。
- `full1.*` 228 项通过；`full2.*` 完整 251 项通过。后续最终 source/sdist/wheel 及真实 wheel 离线安装结果由 `validation.log` 所指新目录记录，不能把三种形态加总为独立业务场景数。
- `focused-final.*` 最终 31 个修复回归通过（增加裸入口场景后完整 suite 为 252 项）。各次失败 validation-attempt 日志和独立 evidence 目录均保留，包括 sdist 裸入口 setup 错误，不以源码全绿代替交付形态复跑。
- `red-probe.*` 保留能力报告与默认禁用不一致的失败；新增确定未发送网络恢复和迟到结果冲突后，最终完整 suite 为 **255 项**。`evidence/validation-bDepdkYZ` 是此前 252 项 source/sdist/wheel 全通过及真实 wheel 安装通过的中间证据；最终 255 项交付以 `validation.log` 指向的下一轮目录为准。

首次额外 wheel 安装验证触发 pip 的 OS 发行信息探测，被内核拒绝读取 `/etc/debian_version`；失败日志和退出码保存在 `validation-attempt1.*`、后续 attempt 日志与 `ensurepip-failure.*`，未放宽隔离。验证辅助脚本 `tools/offline_install_wheel.py` 直接使用当前解释器 stdlib ensurepip 自带的标准 pip wheel，仅将 pip 发行信息探测根设为隔离 HOME，禁用配置发现及网络，用 `--no-index --no-deps --no-cache-dir` 真实安装交付 wheel。不修改宿主/已安装 pip，不替代 wheel 安装逻辑，更不是 SDK mock；`installer-focused.*` 记录真实安装成功。

可重复命令（从候选 repo 根执行，真实服务调用不包含在内）：

```sh
BASE=/opt/data/corpus-build-20261110T170301Z
PYTHON="$BASE/validation-venv/bin/python"
export HOME="$BASE/tmp/repair-home" TMPDIR="$BASE/tmp/repair-tmp"
export CORPUS_AUTHORIZED_SANDBOX_ROOT="$BASE"
export CORPUS_SANDBOX_ROOT="$BASE/tmp/repair-data" CORPUS_DATA_ROOT="$BASE/tmp/repair-data"
export PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 CORPUS_OFFLINE=1
mkdir -p "$HOME" "$TMPDIR"
"$PYTHON" -m pytest tests/safe/test_predeploy_repair.py tests/safe/test_predeploy_adoption.py \
  --sandbox-root "$BASE" --basetemp "$BASE/tmp/repair-recheck" \
  -o cache_dir="$BASE/tmp/repair-cache" --junitxml="$BASE/evidence/repair-20261111/recheck.xml" -q
bash tools/validate.sh --sandbox-root "$BASE" --python "$PYTHON"
```

需要重复旧实现 RED 时，保留原父级 artifact 不变，另行解包覆盖回归文件：

```sh
mkdir -p "$BASE/tmp/repair-red-repeat"
"$PYTHON" -m tarfile -e "$BASE/evidence/validation-zIy3xlXF/dist/corpus_candidate-0.1.0.tar.gz" "$BASE/tmp/repair-red-repeat"
cp tests/safe/test_predeploy_repair.py tests/safe/test_predeploy_adoption.py tests/safe/conftest.py \
  "$BASE/tmp/repair-red-repeat/corpus_candidate-0.1.0/tests/safe/"
cd "$BASE/tmp/repair-red-repeat/corpus_candidate-0.1.0"
"$PYTHON" -m pytest tests/safe/test_predeploy_repair.py tests/safe/test_predeploy_adoption.py \
  --sandbox-root "$BASE" --basetemp "$BASE/tmp/repair-red-repeat-tests" \
  -o cache_dir="$BASE/tmp/repair-red-repeat-cache" --junitxml="$BASE/evidence/repair-20261111/red-repeat.xml" -q
```

已保存补充 RED 的 30 个新场景在该旧 artifact 上失败（退出 1）；随后增加裸入口、确定未发送恢复和迟到结果场景，新实现完整安全套件为 255 个场景。新测试在旧实现缺少 API 的失败与真实行为断言失败分别保留于详细日志，不把 API 缺失当成真实生产故障已复测。

validate.sh 记录每条实际命令、stdout/stderr、独立 `.exit`、JUnit、导入路径、Python/SQLite 环境、artifact hash，执行 source/sdist/wheel 完整 tests/safe、compileall、离线构建、新 venv 实际 wheel 安装与 console/私有目录入口导入。所有 cache/HOME/TMP/DB/报告在 build root 下。历史证据不覆盖。

维护契约的可执行命令（只允许隔离候选路径）：

```sh
"$PYTHON" -m corpus_core.cli --root "$CORPUS_DATA_ROOT" legacy-inventory
"$PYTHON" -m corpus_core.cli --root "$CORPUS_DATA_ROOT" adopt-original --manifest "$BASE/tmp/original-manifest.json"
"$PYTHON" -m corpus_core.cli --root "$CORPUS_DATA_ROOT" adopt-original --manifest "$BASE/tmp/original-manifest.json" --apply
"$PYTHON" -m corpus_core.cli --root "$CORPUS_DATA_ROOT" claim-legacy-profile --manifest "$BASE/tmp/vector-manifest.json"
"$PYTHON" -m corpus_core.cli --settings "$BASE/tmp/isolated-settings.json" embedding-recovery-inventory
"$PYTHON" -m corpus_core.cli --settings "$BASE/tmp/isolated-settings.json" reconcile-embedding --manifest "$BASE/tmp/recovered-response.json"
"$PYTHON" -m corpus_core.cli --settings "$BASE/tmp/isolated-settings.json" call corpus_jobs \
  --json '{"action":"retry","job_id":"EXACT_JOB_ID","operation_id":"EXPLICIT_RESUME_ID"}'
"$PYTHON" -m corpus_core.cli --settings "$BASE/tmp/isolated-settings.json" runner --max-jobs 1
```

这些命令是可执行契约示例，不代表不存在的 manifest 已生成，也不授权真实收费。

## 6. 仍需父级执行的验收

1. 精确 artifact 的真实 Hermes 目录自动发现/六工具 handler；记录 SDK 版本及 `in_process`，无候选路径注入。
2. 明确小额预算后的真实服务完整/零向量/部分向量链路、供应商恢复凭证和计费复核；worker 未运行。
3. 新隔离真实旧库副本迁移/字段保真回归、原件清单与独立身份凭证、可信逐向量来源认领及检索对照、备份恢复；不能复用旧候选全绿结论代替新版验收。
4. 15 个分类未决、2 条孤立向量和历史层级引用逐项人工复核；找不到原件/缺身份证据/混有 mock 的项目保持明确阻断。
5. 若要恢复正式发布/删除/批量采用，另行接入可验证宿主真人审批并独立验收；当前默认禁用是安全降级，不是产品全功能获准上线。

## 7. 修改文件

运行时/打包：`__init__.py`、`build_backend.py`、`corpus_core/adoption.py`（新增）、`corpus_core/cli.py`、`corpus_core/contracts.py`、`corpus_core/db.py`、`corpus_core/embeddings.py`、`corpus_core/management.py`、`corpus_core/probes.py`、`corpus_core/purge.py`、`corpus_core/research.py`、`corpus_core/retrieval.py`、`corpus_core/service.py`、`corpus_core/settings.py`。

验证：`tools/validate.sh`、`tools/offline_install_wheel.py`（新增）、`tests/safe/conftest.py`、`tests/safe/test_predeploy_repair.py`（新增）、`tests/safe/test_predeploy_adoption.py`（新增）、`tests/safe/test_bounded_integrations.py`、`tests/safe/test_m0.py`，以及显式标记旧批准路径模拟的 `test_action_roundtrip.py`、`test_failure_paths.py`、`test_final_review.py`、`test_parent_corrections.py`、`test_purge_boundary.py`、`test_regressions.py`、`test_research.py`、`test_tools.py`（均在 tests/safe 下）。

文档：`README.md`、`docs/ACTION_CONTRACTS.md`、`docs/REQUIREMENTS_TESTS.md`、本报告。未修改原始父级部署失败报告和历史验收证据。
