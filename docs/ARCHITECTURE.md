# ARCHITECTURE.md — corpus-plugin 技术方案（阶段 3）

> 状态: **草案 v0.1** | 2026-09-06 | 输入: docs/PRODUCT.md + docs/review/codex-review-2026-09-05.md + 源码现状
> 本文件是 5 阶段流程（GIT → 产品设计 → **技术方案** → 代码书写 → 项目验证）的阶段 3 产物。
> 目标读者: 阶段 4 执行 agent（claude / codex）/ 维护者 / L。

---

## 1. 架构总览

```
┌──────────────────────── OpenClaw Agent 运行层 ────────────────────────┐
│  plugins/corpus-query (TS)  5 个 corpus_* 工具（参数 schema 不变）    │
│   └─ worker-client: JSONL over Unix socket  →  Python worker          │
│   └─ CORPUS_QUERY_MODE=spawn 降级: spawn python3 cli.py（保留兼容）   │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ IPC: JSONL / Unix socket
┌──────────────────────────────────▼────────────────────────────────────┐
│  corpus/ Python 核心引擎（单一后端）                                   │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐             │
│  │ ingest    │→│ chunk     │→│ embed     │→│ store     │             │
│  │ process-  │ │ canonical │ │ qwen v4   │ │ SQLite +  │             │
│  │ document  │ │ tree       │ │ 1024-dim  │ │ vec0      │             │
│  └───────────┘ └───────────┘ └───────────┘ └─────┬─────┘             │
│  ┌───────────┐ ┌───────────┐ ┌───────────────────▼─────┐             │
│  │ query     │→│ rerank    │ │ worker(常驻, socket 服务) │            │
│  │ recall    │ │ linear/   │ │   query/search/get/score │            │
│  │           │ │ dashscope │ │   /similar/health        │            │
│  └───────────┘ └───────────┘ └─────────────────────────┘             │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ 单一配置源
                              corpus.toml / env
```

**一句话**: TS 插件退化为「协议适配层」（参数校验 + IPC/降级），所有业务逻辑收拢到 Python 核心引擎；在线查询走常驻 worker（消灭每次 spawn 的 100-300ms 冷启动 + 脆弱正则解析），离线 ETL 维持 CLI 形态。

---

## 2. 设计原则（来自产品决策 + Codex 审查 P0）

| # | 原则 | 来源 |
|---|---|---|
| P1 | 单配置源: 消除全部硬编码路径（现 6 处） | PRODUCT / 审查 P0 |
| P2 | 多格式 → canonical schema → chunker，chunker 不直接吃原始解析输出 | 审查 P0 |
| P3 | generation 化 re-chunk，原子切换，绝不静默覆盖 | 审查 P0 |
| P4 | 评分公式版本化（formula_version），权重可审计可回算 | 审查 P0 |
| P5 | 在线召回 = 常驻 Python worker + IPC；spawn 仅作 feature-flag 降级 | 审查架构结论 A |
| P6 | 可观测性: ingestion_jobs 状态机 + fsck 一致性校验 | 审查 P0 |
| P7 | 冷启动可见性: prior_only 不得伪装已审核 | 审查 P0 |
| P8 | 向后兼容: 旧 corpus.db / 旧 chunk_id / 旧工具参数 schema 不破坏 | PLAN 风险表 |

---

## 3. 模块切分（边界）

| 层 | 目录 | 职责 | 不做什么 |
|---|---|---|---|
| 配置 | `corpus/config.py` + `corpus.toml` | 路径/embedding/query 参数解析 | 不写 openclaw.json |
| 核心引擎 | `corpus/` | ingest / chunk / embed / store / query / score | 不感知 OpenClaw |
| 常驻服务 | `corpus/worker.py` | socket IPC 服务，命令分发，健康检查 | 不含业务逻辑，只调度 |
| IPC 协议 | `corpus/ipc.py` | JSONL 帧编解码、错误码、幂等 request_id | — |
| 插件适配 | `plugins/corpus-query/src/` | 工具注册 + worker-client + spawn 降级 | 不重复业务逻辑 |
| Skill | `docs/SKILL-corpus-prisma.md` | PRISMA 使用 SOP（指向新命令） | 不承载实现细节 |

**现状 → 目标迁移关注点**:
- `chunk_query.py` 与 `cli.py` 的召回逻辑合并进 `corpus/query.py`（消除双后端: 现在是 cli.py + chunk_query.py 两套）
- `plugins/corpus-query/chunk_query.py` 作为 worker 的 chunk 树能力并入 `corpus/`（get/similar 命令进 worker）

---

## 4. 配置与路径管理（P1: 消除硬编码）

### 4.1 配置源

解析优先级（高→低）:
1. 环境变量: `CORPUS_TOML`（显式指定配置文件）
2. `corpus.toml`（默认位置: `$CORPUS_HOME/corpus.toml`，`CORPUS_HOME` 缺省 `~/.openclaw/corpus`）
3. 内置默认值

### 4.2 corpus.toml 结构（草案）

```toml
[paths]
corpus_db = "~/.openclaw/workspace/projects/_corpus/corpus.db"  # 兼容旧数据
vec_ext = "auto"        # auto=从 sqlite-vec python 包探测；或显式绝对路径
worker_socket = "/tmp/corpus-worker.sock"   # 在线召回 socket

[embedding]
provider = "qwen"
model = "text-embedding-v4"
dim = 1024

[query]
rerank_default = "linear"       # linear | dashscope
vec_weight = 0.6                # hybrid 融合权重（现状值）
kw_weight = 0.4

[scoring]                        # 新增（P4）
prior_weight = 0.3
evidence_weight = 0.7
```

### 4.3 消除清单（6 处硬编码 → 全部经 config 解析）

| 现状硬编码 | 文件 | 改为 |
|---|---|---|
| `CLI_PATH = "/root/.openclaw/workspace/scripts/corpus/cli.py"` | index.ts | 删除（TS 不再直接 spawn cli）|
| `CHUNK_HELPER_PATH = ...` | index.ts | 删除（走 worker）|
| `CLI_TIMEOUT_MS = 120_000` | index.ts | worker.request_timeout（config）|
| `CORPUS_DB = Path("/root/...corpus.db")` | chunk_query.py | `config.paths.corpus_db` |
| `VEC_EXT = "/usr/local/lib/...vec0.so"` | chunk_query.py | `config.paths.vec_ext`（auto 探测）|
| `UNIFIED_CORPUS_DB`（cli.py 内） | cli.py | `config.paths.corpus_db` |

> vec_ext auto 探测: `import sqlite_vec; sqlite_vec.get_loadable_path()`（pip 安装 sqlite-vec 后可用，替代硬编码 npm 路径——该路径会随 OpenClaw 升级失效）。

---

## 5. Canonical Schema（P2: 多格式中间表示）

### 5.1 为什么需要

DOCX 样式名≠语义 heading、HTML 有导航/脚本/隐藏节点、Docling 版本升级会改节点类型。若直接拼 Markdown 喂 chunker，同文档重处理 chunk 边界会漂移。

### 5.2 Canonical 格式: 规范化 JSON 树（.canon.json）

```json
{
  "doc_id": "33875643",
  "content_hash": "sha256:ab12...",
  "parser": "docling",
  "parser_version": "2.124.0",
  "source_file": "knowledge/fulltext/33875643.pdf",
  "nodes": [
    { "kind": "heading", "level": 1, "sec_num": "1", "title": "Introduction", "text": "" },
    { "kind": "paragraph", "path": "/1/1", "text": "EGFR degradation ..." },
    { "kind": "table",     "path": "/2/1", "markdown": "| Tx | N | HR |", "caption": "Table 1" },
    { "kind": "formula",   "path": "/2/2", "latex": "\\frac{a}{b}" },
    { "kind": "caption",   "path": "/3/1", "text": "Figure 2. ..." }
  ],
  "parser_warnings": ["html nav stripped", "table 3 col widths lost"]
}
```

### 5.3 生产管线

```
process-document --format auto|pdf|docx|html|jats|md
  → 原始解析（docling / jats_to_md）
  → canonicalizer（清洗 + heading 规范化 + source locator + warnings）
  → <.canon.json> 落盘（与 raw 同目录，<pmid>.canon.json）
process-raw --pmid <pmid>
  → 读 .canon.json（优先）或 .md/.txt（legacy）
  → chunker 只吃 canonical
  → embed + store（generation 化，见 §6）
```

- `process-document` 替换 `process-pdf`（保留 `--pdf` 别名兼容）
- canonicalizer 规则: 首个 H1 定为根、跳级压缩、空标题剔除、HTML selector 黑名单（nav/script/style/ad）、DOCX 样式名→heading 映射表
- golden fixtures: 每格式 ≥10 份（阶段 5 验收门槛，标题树准确率 ≥95%）

---

## 6. 数据层设计（P3/P6: generation + 可观测性）

### 6.1 Schema 演进（v2 → v3）

**documents 表新增列**:
```sql
ALTER TABLE documents ADD COLUMN content_hash TEXT;      -- sha256(raw 内容)
ALTER TABLE documents ADD COLUMN generation INTEGER DEFAULT 1;  -- active 代
ALTER TABLE documents ADD COLUMN parser_version TEXT;    -- docling 版本
ALTER TABLE documents ADD COLUMN chunker_version TEXT;   -- chunker 版本
```

**chunks / chunk_vectors 新增列**:
```sql
ALTER TABLE chunks ADD COLUMN generation INTEGER DEFAULT 1;
-- chunk_vectors 是 vec0 虚拟表，generation 通过 chunk_id 前缀维护:
--   chunk_id = "<pmid>__<gen>__<path>__<part>"
```

**新增 ingestion_jobs 表（P6 可观测性）**:
```sql
CREATE TABLE IF NOT EXISTS ingestion_jobs (
  job_id TEXT PRIMARY KEY,
  pmid TEXT NOT NULL,
  stage TEXT NOT NULL,            -- discovered|parsed|chunked|embedded|indexed|failed
  status TEXT NOT NULL,           -- pending|running|done|failed
  error TEXT,
  retry_count INTEGER DEFAULT 0,
  input_hash TEXT,
  parser_version TEXT, chunker_version TEXT, embedder_version TEXT,
  created_at TEXT, updated_at TEXT
);
```

**quality_evidence 表新增列（P4 评分版本化）**:
```sql
ALTER TABLE quality_evidence ADD COLUMN formula_version TEXT DEFAULT 'v1';
ALTER TABLE quality_evidence ADD COLUMN prior_weight REAL;
ALTER TABLE quality_evidence ADD COLUMN evidence_weight REAL;
ALTER TABLE quality_evidence ADD COLUMN scorer_id TEXT;
ALTER TABLE quality_evidence ADD COLUMN quality_status TEXT DEFAULT 'evidence';
-- quality_status: evidence | prior_only （P7: prior_only 不得伪装已审核）
```

**relevance 表新增列（陈旧传播）**:
```sql
ALTER TABLE relevance ADD COLUMN generation INTEGER DEFAULT 1;
ALTER TABLE relevance ADD COLUMN stale INTEGER DEFAULT 0;
```

### 6.2 写入事务语义

- vec0 表 `INSERT OR REPLACE` 无效 → 必须 DELETE + INSERT（现状已知）
- 写操作: 单 writer（worker 内互斥锁 / CLI 单进程），连接 `PRAGMA busy_timeout=30000`
- 事务边界: chunks + chunk_vectors + generation 指针 + ingestion_jobs 状态 **同一事务提交**
- embedding API 失败 → job 留 `stage=embedded, status=failed` 可重试，**不得**留下"已索引"虚状态

---

## 7. 在线召回: Python Worker + IPC（P5）

### 7.1 选型结论（采纳 Codex 审查）

- **A 常驻 Python worker + Unix socket JSONL**: 首选（消灭 spawn 冷启动、成熟 sqlite-vec 生态、无 native Node ABI 风险）
- **C spawn 降级**: 保留为 feature flag（`CORPUS_QUERY_MODE=spawn`），worker 不可用时自动降级
- **B better-sqlite3**: 否决（native 编译/ABI/安装迁移风险，node:sqlite loadExtension 被禁已堵死纯 TS 直连）

### 7.2 协议（JSONL over Unix socket）

请求（每行一个 JSON）:
```json
{"request_id": "u1-1725", "cmd": "query", "params": {"project": "ar-review", "query": "EGFR", "top_k": 10}}
```

响应:
```json
{"request_id": "u1-1725", "ok": true, "result": {...}}
{"request_id": "u1-1725", "ok": false, "error": {"code": 3, "message": "db unavailable"}}
```

命令集（对齐 5 个工具 + 运维）:
| cmd | 对应工具 | 说明 |
|---|---|---|
| `query` | corpus_query | 召回+评分+推荐（含 rerank）|
| `search_self` | corpus_search_self | 自写文档检索 |
| `score` | corpus_score | 写评分 + 回算 quality_final（JSON 响应，**替代正则解析**）|
| `get_chunk` | corpus_get_chunk | 树上下文 |
| `similar_level` | corpus_list_similar_level | 同层级平行 section |
| `health` | — | 健康检查（worker 存活 + vec0 加载自检）|
| `fsck` | — | 一致性校验（§8.2）|

错误码: 0 ok / 1 not_found / 2 invalid_args / 3 db_error / 4 embedding_error / 5 timeout / 6 worker_busy

### 7.3 Worker 生命周期

- 启动: `corpus worker --socket /tmp/corpus-worker.sock`（daemon 由插件或 a2a_wake 拉起）
- 空闲退出: 300s 无请求自退（防僵尸）;插件检测 socket 失效 → spawn 降级 → 空闲重试拉起
- 超时: 请求 30s;health 10s;embedding/rerank 调用 60s
- 幂等: 查询幂等;写操作（score）按 request_id 去重，连接断开重试一次
- 并发: 读并发（SQLite WAL）;写互斥（单 writer）
- 启动自检: vec0 加载、维度、距离度量写入诊断日志（审查建议）

### 7.4 插件侧改造（index.ts）

1. 删除 CLI_PATH/CHUNK_HELPER_PATH 硬编码 + runPython spawn 逻辑（降级路径除外）
2. 新增 worker-client: 连接 socket → 发 JSONL → 收响应 → 超时/重试
3. `CORPUS_QUERY_MODE=spawn` 时: 回退现有 spawn 逻辑（**保留旧路径一段兼容期**，PLAN 风险缓解）
4. score 工具: 解析 worker JSON（删掉脆弱正则 `/^\[score\]/`）
5. 5 个工具参数 schema **不变**（向后兼容，P8）

---

## 8. 一致性（P6: fsck + 陈旧传播）

### 8.1 Re-chunk 状态机（generation 原子切换）

```
process-raw --pmid <X>
  ├─ 读 raw → 计算 content_hash，与 documents.content_hash 比对
  ├─ 未变 & chunker_version 未变 → SKIP（幂等，不重切）
  ├─ 变了:
  │   ├─ generation+1 → 写新 chunks（chunk_id 带新 gen 前缀）+ 新向量
  │   ├─ 校验: chunks↔vec0 一一对应（count 比对）
  │   ├─ 原子切换 documents.generation = new（同一事务）
  │   └─ 异步清理旧 generation chunks/vec（保留 1 代回滚窗口）
  └─ relevance/quality_evidence 依赖旧 chunk 的 → 标 stale（generation 不匹配）
```

### 8.2 fsck 命令

- chunks ↔ chunk_vectors 一一对应（查缺失/重复/orphan）
- chunk_id 维度校验（embedding 维度 = config.embedding.dim）
- 树完整性: parent_id/child_ids/sibling_ids 交叉引用无悬空
- 输出报告 + `--fix` 选项（孤儿向量清理）

### 8.3 陈旧传播规则

| 变更 | 影响 | 处理 |
|---|---|---|
| documents.article_type/prior 修改 | quality_evidence | 标 stale → 按 formula_version 重算 |
| 重切片（generation+1） | relevance / chunks-vec | relevance 标 stale; 查询只认 active generation |
| embedder 版本变更 | 全部向量 | 全量重嵌（reembed_project.py 已有雏形），期间旧代可读 |

---

## 9. 评分系统（P4: formula_version）

### 9.1 公式版本

| 版本 | 公式 | 状态 |
|---|---|---|
| v1（现状） | `quality_final = max(prior_num, evidence_mean)` | 冻结（历史可读）|
| v2（新，拍板） | `quality_final = 0.7×evidence_mean + 0.3×prior_num`; 无打分 → `prior_num` + `quality_status=prior_only` | 阶段 4 实现 |

- `evidence_n < 3` 时: 按实际特征数均值，写 `quality_status=partial`，默认不进 primary（除非显式 override）
- `evidence_n = 0` → prior_only（冷启动可见，P7）

### 9.2 迁移

```
corpus recompute-scores --formula v2 [--project <slug>]
  → 遍历 quality_evidence，按 formula_version 重算 quality_final
  → 写 formula_version/prior_weight/evidence_weight
  → 旧 v1 行保留公式历史（不覆盖删除，append 新版本行或原位更新+记录）
```

- `make_recommendation` 阈值（primary ≥0.55 & ≥0.75; supporting ≥0.50）**不变**，但输入 quality_final 来源变 v2 → 推荐分布会变化（审查已指出，属预期）
- 阶段 5 用标注集校准阈值（PR 曲线）→ 校准结果写 config.query/`[scoring]`

---

## 10. 接口契约（TS 插件 ↔ worker ↔ CLI）

### 10.1 工具 ↔ 命令映射（现状 → 目标）

| 工具 | 现状调用 | 目标调用 | 破坏性 |
|---|---|---|---|
| corpus_query | spawn cli.py query | worker query | 无（schema 不变）|
| corpus_search_self | spawn cli.py search-self | worker search_self | 无 |
| corpus_score | spawn + 正则 | worker score (JSON) | 无 |
| corpus_get_chunk | spawn chunk_query.py get | worker get_chunk | 无 |
| corpus_list_similar_level | spawn chunk_query.py similar | worker similar_level | 无 |

### 10.2 CLI 命令对齐（阶段 4 完成后）

| 命令 | 状态 |
|---|---|
| `init / register-raw / process-document / process-raw / ingest-doc / search-self / answer-self / query / score / group / list-*` | 保留（process-pdf → process-document 别名）|
| `worker / fsck / recompute-scores` | 新增 |

---

## 11. 实施计划（阶段 4 任务拆分 → 版本）

| 里程碑 | 内容 | 版本 |
|---|---|---|
| M1 | config 层（corpus.toml + config.py）+ 6 处硬编码消除 | v0.2.0 |
| M2 | canonical schema + process-document 多格式入口 + golden fixtures 初版 | v0.2.0 |
| M3 | worker + IPC 协议 + TS worker-client + spawn 降级 | v0.3.0 |
| M4 | generation/ingestion_jobs/fsck + re-chunk 状态机 | v0.3.0 |
| M5 | 评分 v2 + recompute-scores 迁移 + prior_only | v0.4.0 |
| M6 | 测试补齐（db/cli/worker/canonicalizer）+ 单测 | v0.4.0 |
| M7 | E2E 验证 + 文档同步（阶段 5） | v1.0.0 |

**依赖**: M1 → M2/M3 可并行; M3 → M4; M5 依赖 M1; M6 全覆盖。

**禁止事项**（贯穿）: 不改 openclaw.json / 不重启 gateway / 不装插件 / 保留旧路径兼容期。

---

## 12. 版本兼容策略（P8）

| 兼容对象 | 策略 |
|---|---|
| 旧 corpus.db（v1/v2 schema） | 启动时自动迁移（migrate_v2.sql 已有; v3 列 ALTER 幂等）|
| 旧 chunk_id（V1 格式） | get/similar 接受新旧两种格式 |
| 旧工具参数 | 5 个工具 schema 不变 |
| 旧 Python 路径 | spawn 降级路径保留一段兼容期 |
| 旧 fact-infra | 完全不碰（产品边界，fact 独立）|

---

*下一步: L 确认/调整本方案 → 阶段 5 验收门槛对齐 → 进入阶段 4（代码书写）逐里程碑实施。*