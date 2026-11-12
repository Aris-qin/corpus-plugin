# 可重建的本地向量缓存

数据库 `chunk_embeddings.vector` 的 JSON 是唯一真源；本缓存不是索引迁移，不使用或认领 `legacy_vectors`，不调用 embedding/rerank 服务。删除 `runtime/vectors` 后仍可走原 JSON 路径，并可从数据库重建。

## 运维

```sh
python -m corpus_core.cli --root "$CORPUS_DATA_ROOT" --sandbox-root "$CORPUS_AUTHORIZED_SANDBOX_ROOT" vectors-build --profile-id "$PROFILE_ID"
python -m corpus_core.cli --root "$CORPUS_DATA_ROOT" --sandbox-root "$CORPUS_AUTHORIZED_SANDBOX_ROOT" vectors-build --profile-id "$PROFILE_ID" --apply
python -m corpus_core.cli --root "$CORPUS_DATA_ROOT" --sandbox-root "$CORPUS_AUTHORIZED_SANDBOX_ROOT" vectors-verify --profile-id "$PROFILE_ID"
```

构建默认 dry-run，`--apply` 才发布文件。操作必须在明确授权的根目录执行；本次验证只允许隔离副本，不能把以上示例用于生产。运维命令不是模型工具。

## 格式

文件前缀为 `<profile_id>-<fingerprint>`，位于经 `paths.bounded` 校验的 `runtime/vectors/`。

- `.f32`：无文件头的 float32 little-endian 行主序原始矩阵，长度必须严格等于 `count * dim * 4`。行按 `chunk_id` 升序排列。原始浮点数不做预归一化。
- `.json`：`schema=1`、`profile_id`、`dim`、`count`、`dtype=float32-le`、`fingerprint`、`created_at`、`index_sha256`，额外包含 `data_sha256`。
- `index_sha256`：对每行 `encoded([chunk_id, zero_based_row_index]) + '\n'` 的 UTF-8 字节按顺序更新 SHA-256。
- `data_sha256`：覆盖整个原始矩阵。无独立二进制魔数；元数据的 schema/dtype 是格式标识，数据长度与 SHA-256 用于完整性校验。
- 指纹是 `count(*)`、`max(rowid)` 和 profile config digest 的稳定摘要。不同指纹有不同文件名；旧文件不自动删除。

构建在数据库读快照内流式转码，临时文件 flush/fsync 后分别 `os.replace` 发布数据与元数据。文件对不是跨文件事务；中断窗口可能只发布了其中一个文件，读取者此时必须校验并降级，不会消费不一致的二进制。相同内容重复构建校验成功后不更改文件。

## 读取与降级

读取先确定数据库指纹，再 mmap，并通过 `memoryview.cast('f')` 取行。冷加载重建 chunk ID 到行号字典，核对索引与数据摘要，并从量化后的行计算范数。计算核唯一接缝为 `corpus_core.vector_cache.dot_kernel(query_vector, matrix_view, row_index)`；调用者负责除以双方范数。替换此函数无需改检索调用方。大端环境不能直接使用该 cast，因此安全退回 JSON。

`CORPUS_VECTOR_CACHE_BYTES` 默认为 `268435456`，有效范围 `0..4294967296`。该预算计量驻留 mmap 文件的字节数，Python 字典、范数和其他对象不是这项文件字节预算的一部分；不能将其理解为整个进程的 RSS 上限。LRU 按产物绝对路径缓存，超限整件淘汰并释放 memoryview/mmap；`0` 会清空映射并使用 JSON。单件产物大于预算不缓存。租用映射期间持有重入锁，防止其他线程淘汰正在使用的视图（也意味着同进程二进制扫描串行）。

| 条件 | warning | 行为 |
| --- | --- | --- |
| 未构建、预算为零、产物大于预算 | `vector_cache_unavailable` | JSON |
| 当前指纹产物不存在、发现旧指纹产物 | `vector_cache_stale` | JSON |
| 缺少文件对的一半、长度/格式/索引/数据摘要不符、加载或计算异常 | `vector_cache_corrupt` | JSON；若扫描中失败，丢弃该轮候选并从头扫描 |
| 产物可用 | `vector_binary_scan` | eligible SQL 不变，仅跳过 JSON 向量列，改从映射取分 |

热加载检查文件 inode/大小/修改与变更时间，变化后重新验证。廉价数据库指纹不发现同数量、同最大 rowid 的原位修改；`vectors-verify` 重新计算索引摘要并将数据库 JSON 逐行转 float32，核对数据摘要，以发现这种修改。运维验证失败应重建；本缓存不宣称能自动检测所有数据库原位修改。哈希用于意外损坏检查，不是针对同权限攻击者的认证机制。

float32 会改变浮点分数，不能保证所有数据上的顺序完全相同；必须参考真实副本测量报告中的精度与 top-200 结论。分页、过滤、版本优先级、rerank、RRF 回退和截断上界逻辑不在本优化的变更范围内。

## 离线规模测量

先以 `cp --reflink=auto` 将授权的真实库复制到隔离工作根目录的 `tmp/vec-real/catalogue.sqlite`，只在该副本构建产物，再通过标准安全测试入口运行：

```sh
CORPUS_OFFLINE=1 CORPUS_VECTOR_MEASURE_ROOT="$CORPUS_AUTHORIZED_SANDBOX_ROOT/tmp/vec-real" PYTEST_ADDOPTS='-k real_copy_measurement -s' ../run-tests.sh
```

测量代码 `tools/vector_benchmark.py` 以 SQLite `mode=ro` 打开副本，不运行 schema 初始化。每种路径启动独立子进程运行多次；首次二进制扫描标记为进程冷缓存，后续为热缓存（不清空操作系统页缓存）。输出包含逐次阶段时间、总时间、精度、数据库前后摘要与 Linux RSS 高水位。内存通过 `getrusage` 系统调用读取，不访问隔离根目录之外的 `/proc` 文件；它是 Linux 进程 RSS 高水位，不是每次查询的瞬时增量。p95 使用 nearest-rank，小样本包含首次冷加载。
