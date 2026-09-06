# Stage 4 第一波实现审计（2026-09-06）

审计对象：`1cca9e4, a70df0b, cf62284, fbca971, 6958f5b`。结论为 **FAIL，暂不能进入第二波**。

## §1 canonical — FAIL

实现模型和大部分 Markdown/TXT 规则存在（`corpus/canonical.py:31-40`，791 行），碰撞扩展、树校验、warning error 拒绝及适配器测试也有覆盖。但阶段门要求每种格式至少 10 组 golden fixtures；仓库实际仅 `md` 2 组、`txt` 1 组（共 6 个 input/json 文件），没有 `pdf/docx/html/jats` 目录，远低于契约要求。更严重的是生产 CLI 的 `cmd_process_raw` 仍按后缀直接调用旧 chunker（`corpus/cli.py:407-421`），并未优先读取 `.canon.json`，也没有调用 `canonical.process_raw`；这直接违反“chunker 只接受 canonical”及 process-raw dispatch 契约（P0）。`canonical.py` 自身将真实 docling/JATS 解析留为 NotImplemented（模块说明 `canonical.py:16-18`），在本阶段可接受为阶段 5 边界，但不能抵消 fixture/入口缺项。

## §2 chunk_id / generation / vec0 — PARTIAL

`generation.py` 提供长度前缀 codec、legacy 只读保护、partial unique index 和基本切换状态机（`generation.py:38-63, 150-180`），`db.vector_search` 的 vec0→chunks→manifest JOIN 顺序及 active 过滤正确（`db.py:366-410`）。测试对特殊路径和 retired 代有实值断言。

缺项/风险：契约签名没有 `include_retired`，实现额外暴露该绕过 active 的参数（`db.py:371,386`）；若上层误传即可查询 retired，审计通道并未隔离为独立 API（P1）。`chunk_query.py` 仍使用硬编码生产 DB 与 vec 扩展路径（`plugins/corpus-query/chunk_query.py:21-22`），且其查询未复用 generation-aware helper，存在回归风险（P1）。

## §3 评分 v3 — PARTIAL

v3 DDL 字段、唯一 idempotency key、current 表和 tie-break 逻辑均实现（`db.py:131-159, 481-585`）；测试验证并列时间戳、重试不增行、override 字段校验及 v1/v2 共存。

但阶段测试清单要求迁移去重、checkpoint 异常恢复、TS JSON schema、推荐过滤等测试；本提交没有这些测试或实现证据。现有测试仅调用 Python DB API，不能证明 CLI/worker/TS 全链路契约。属于 P1（阶段门未满足），并非已证明的功能正确性。

## §4 backup — PARTIAL

使用 SQLite backup API、UTC 时间戳、完整性检查、失败清理和幂等重跑（`corpus/backup.py:37-90`），相关测试断言值而非仅存在性。潜在实现问题：源连接以 `mode=ro` 打开后执行 `PRAGMA wal_checkpoint(PASSIVE)`（`backup.py:67`）；该 pragma 可能需要写权限，在 WAL/只读连接上会失败，导致正常热备失败（P1）。恢复 runbook 要求的 writer 停止、quarantine WAL/SHM、vec0 fsck 和 worker gate 并未在该文件中实现；docstring 也明确 vec0 fsck 留到 stage 5（`backup.py:104-108`），因此阶段门仍未满足（P1）。

## §5 IPC — PARTIAL

`FrameReader` 能处理任意半包、换行分帧及 1 MiB 上限（`ipc.py:70-117`）；`ipc_idempotency` DDL 含完整 response_json/hash、processing/committed/failed 状态（`ipc.py:130-145`），reserve/lease/过期重试测试断言正文相等和行数不变。

但仅实现 framing/cache 库，没有契约要求的 worker dispatch、stale socket/pidfile/flock、STARTING/READY/DEGRADED/BACKOFF 状态机或 TS score_event_id 生成；测试也完全未覆盖这些项目。故为 P1 阶段缺项。

## 测试自证循环与回归检查

- 未发现 `xfail`/`pytest.mark.skip` 软化契约；`conftest.py:19-23` 仅忽略一个历史报告脚本 `test_chunker.py`，不是契约测试。
- 契约测试存在明显覆盖缩水：golden 仅 3 组而非 6 格式×10；§3/§4/§5 的迁移、worker、TS、恢复中断测试全部缺失。多数断言是真值断言，但“实现了 API”被当作全链路契约的替代。
- 全仓 `vector_search` 调用点仅 `corpus/cli.py:667,832,1060` 使用旧参数（未传 generation）；虽然因默认 active JOIN 不会立即报错，但 CLI 没有 generation 上下文，process/rechunk 路径可能查询错误代（P1）。
- `cmd_process_raw` 仍直调旧 Markdown/plain-text chunker（上述 P0），是确定的回归/契约违反。

## 红线

5 个 commit 的 `git diff --name-only` 未包含 `plugins/corpus-query/src/index.ts`、`openclaw.json` 或生产库路径；未执行任何写入生产库操作。Git 对仓库外生产库无法提供 diff，但审计过程为只读。

## 严重度汇总与结论

- **P0**：CLI process-raw 未走 canonical 优先入口，仍直接调用 legacy chunker。
- **P1**：fixtures 严重不足；worker/TS/恢复 runbook 阶段门缺失；`vector_search` 暴露 retired 绕过参数；chunk_query 硬编码路径且不复用 generation helper；只读连接 checkpoint 风险。

因此第一波实现不能放行进入第二波（`plugins/corpus-query/src/index.ts` + `worker.py`）。应先修复 P0，并补齐各节自动化测试及 229MB fixture/恢复验收，再重新审查。
