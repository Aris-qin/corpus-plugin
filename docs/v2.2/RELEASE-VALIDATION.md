# Corpus v2.2.0 发布核验

日期：2026-11-12。

本版在 v2.0.0 基础上前置四项修复：截断误报、RRF 排序回退、embedding 429 归类、二进制向量产物与 schema 扁平化。版本标识统一为 2.2.0（`pyproject.toml`、根 `plugin.yaml`、adapter `plugin.yaml`），SQLite schema 仍为 v4。

## 本轮实际执行

完整矩阵：`tools/validate.sh --sandbox-root /opt/data/corpus-vec-a-20261112 --python /opt/data/corpus-validation-venv/bin/python`

- 源码：397 passed，1 skipped。
- sdist 解包：397 passed，1 skipped。
- wheel 解包：396 passed，2 skipped。
- 每种形态额外 20 次真实进程中断恢复，共 60 次通过。
- Python 编译、构建、新 venv 离线 wheel 安装、CLI probe 与目录入口导入均通过。
- 产物：`corpus_candidate-2.2.0-py3-none-any.whl`、`corpus_candidate-2.2.0.tar.gz`；证据目录 `evidence/validation-1flbRkz4`。

wheel 形态多出的一次跳过来自 adapter 一致性检查：wheel 不携带 `plugins/hermes-corpus-query` 树，该用例按形态跳过。

## 本轮矩阵发现并修复的两处打包缺陷

矩阵并非一次通过，以下两处是真实缺陷，已修后重跑：

1. `tests/safe/test_vector_real_measurement.py` 顶层导入 `tools.vector_benchmark`，而 wheel 矩阵此前只复制 `tests/`，导致收集错误。修复：测试改为 `pytest.importorskip`，`validate.sh` 同步复制 `tools/`。
2. `test_host_adapter_contracts_match_candidate` 假定 adapter 树存在，在 wheel 形态下 `FileNotFoundError`。修复：adapter 缺失时跳过，并保留源码/sdist 形态的字节一致性断言。

## 与已部署版本的关系

- 插件目录（`plugins/hermes-corpus-query`）与线上安装的 `/home/hermeswebui/.hermes/plugins/corpus-query` 逐文件一致，唯一差异是 `plugin.yaml` 版本号（2.2.0 对 0.1.0）。
- 专用运行时 `/opt/data/corpus-runtime-v1` 仍有漂移：`contracts.py`（缺 `schema_union`）与 `embeddings.py`（429 归类）为本轮之前版本。工具注册 schema 取自插件目录，因此扁平化在线上生效；执行路径不受影响。该漂移不在本次 GitHub 发布范围内。

## 不能混淆的历史证据

本轮为离线测试，未新增计费 embedding/rerank 请求，未重跑真实库迁移。此前本地部署阶段执行的真实 DashScope 全链路、真实旧库副本迁移与回退、42,548 切片重嵌与二进制产物精度核对，仍属于那一阶段的证据；本次通过代码一致性承接，不宣称重新执行。

本版仍未完成可信真人审批。正式报告发布、批量正式采用、合并、永久删除继续默认禁用；测试中的审批模拟不是真人授权。`legacy_vectors` 42,498 条保持隔离未认领。

## 发布范围

发布源码、sdist、wheel、插件 ZIP 与 SHA256 清单。未修改 GitHub Actions：当前令牌不含 workflow 权限，不宣称远程 CI 通过。

本次发布不重启网关、不修改生产数据库、不解锁敏感动作。验证期间生产库 `catalogue.sqlite`（2.86 GB）mtime 保持 2026-11-12 07:44:53 UTC 未变，本轮矩阵全部在 `/opt/data` 沙箱内执行。
