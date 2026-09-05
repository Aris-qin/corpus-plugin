# ARCHITECTURE.md — corpus-plugin 技术方案（阶段 3）

> 状态: **草案 v0.2（Codex 审查 P0 补丁版）** | 2026-09-06 | 输入: docs/PRODUCT.md + docs/review/codex-review-2026-09-05.md + docs/review/codex-arch-review-2026-09-06.md + 源码现状
> 本文件是 5 阶段流程（GIT → 产品设计 → **技术方案** → 代码书写 → 项目验证）的阶段 3 产物。
> 目标读者: 阶段 4 执行 agent（claude / codex）/ 维护者 / L。
> 修订记录: v0.1 → v0.2 按 codex-arch-review-2026-09-06.md 闭合 4 个 P0（canonical/generation/迁移/评分）+ P1（IPC 状态机）。

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
| P4b | 评分 append-only 历史行 + current 指针,不原地更新（v1/v2 共存） | codex-arch-review P0 |
| P5 | 在线召回 = 常驻 Python worker + IPC；spawn 仅作 feature-flag 降级 | 审查架构结论 A |
| P6 | 可观测性: ingestion_jobs 状态机 + fsck 一致性校验 | 审查 P0 |
| P7 | 冷启动可见性: prior_only 不得伪装已审核 | 审查 P0 |
| P8 | 向后兼容: 旧 corpus.db / 旧 chunk_id / 旧工具参数 schema 不破坏 | PLAN 风险表 |
| P9 | IPC 幂等持久化 + socket 单实例/权限 + 超时后查状态 | codex-arch-review P1 |
| P10 | vector 写入统一 DELETE+INSERT（现状 db.py 与 cli.py 语义矛盾,codex 抓出） | codex-arch-review P0 |

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
  "schema_version": "1.0",
  "doc_id": "33875643",
  "content_hash": "sha256:ab12...",
  "parser": "docling",
  "parser_version": "2.124.0",
  "source_file": "knowledge/fulltext/33875643.pdf",
  "nodes": [
    { "node_id": "n1", "kind": "heading", "level": 1, "sec_num": "1", "title": "Introduction", "text": "", "ordinal": 1, "parent_id": null, "path": "/1", "char_start": 0, "char_end": 0 },
    { "node_id": "n2", "kind": "paragraph", "path": "/1/1", "text": "EGFR degradation ...", "ordinal": 2, "parent_id": "n1", "char_start": 120, "char_end": 480 },
    { "node_id": "n3", "kind": "table", "path": "/2/1", "markdown": "| Tx | N | HR |", "caption": "Table 1", "ordinal": 5, "parent_id": null, "char_start": 3200, "char_end": 3900 },
    { "node_id": "n4", "kind": "formula", "path": "/2/2", "latex": "\\frac{a}{b}", "ordinal": 6, "parent_id": null, "char_start": 3910, "char_end": 3930 },
    { "node_id": "n5", "kind": "caption", "path": "/3/1", "text": "Figure 2. ...", "ordinal": 9, "parent_id": null, "char_start": 5000, "char_end": 5100 }
  ],
  "parser_warnings": [
    { "level": "warn", "msg": "html nav stripped" },
    { "level": "info", "msg": "table 3 col widths lost" }
  ]
}
```

### 5.2.1 节点必需字段（chunker 消费契约，全部必填）

| 字段 | 类型 | 说明 | chunker 用途 |
|---|---|---|---|
| `node_id` | str | 文档内唯一稳定 ID（`n<ordinal>`，解析顺序号） | parent/child 引用锚点 |
| `kind` | enum | heading \| paragraph \| table \| formula \| caption \| list \| footnote | 结构识别/切片规则 |
| `level` | int | heading 层级（1-6; 非 heading 为 0）| heading_chain/层级过滤 |
| `sec_num` | str \| null | 章节号（"1", "2.1"）;无则 null | section_number/title |
| `title` | str | heading 文本（非 heading 为空串）| heading_path 构造 |
| `text` | str | 正文/标题文本（table 为 canonical markdown 行）| chunk 正文 |
| `ordinal` | int | 全局解析顺序号（单调递增）| chunk 顺序/ordinal |
| `parent_id` | str \| null | 父节点 node_id（树根为 null）| parent/child_ids 重建 |
| `path` | str | 规范化树路径 `/1/2/1`（层序路径，非原文位置）| heading_path/层级 |
| `char_start`/`char_end` | int | 原文 char 定位（parse 层输出，供 source locator）| 可回溯性（阶段 5 验收）|

**树不变量**（canonicalizer 必须保证）:
1. `parent_id` 引用存在的 node_id;所有非根节点有且仅有一个父
2. `ordinal` 全局严格递增;`path` 与 parent 链一致（子 path = 父 path + 序号）
3. 同级重复标题可区分（`path` 序号不同）;跳级 heading 压缩（H1→H3 视为 H2）
4. list/footnote/table/formula 各有 `kind`,不得混入 paragraph text
5. 段落拆分规则: 不跨 heading 边界,单节点建议 ≤2400 字符（超长由 chunker 二次切,标 `split` warning）

### 5.2.2 表格/公式/图片 canonical 文本化

- **table**: `markdown` 列存 GFM 表格行（`| a | b |`）;列宽/合并信息丢失记入 `parser_warnings`(info 级);caption 独立节点
- **formula**: `latex` 列存 LaTeX;不混入 text;display/inline 由 `kind` 区分（formula / inline_formula）
- **图片**: `kind=figure` + `alt` 文本 + `caption` 独立节点;无 OCR（Non-Goal）
- **footnote**: `kind=footnote`,`text` 存全文,`ref_ordinal` 指向引用处 ordinal

### 5.2.3 文件布局与命名（raw / canon 分离,绝不改原始文件）

```
<raw_root>/<pmid>/<pmid>.<ext>            # 原始文件（register 时命名,ext ∈ pdf/docx/html/xml/md/txt）
<canon_root>/<pmid>/<pmid>.canon.json     # canonical（与 raw 分离目录,默认 <raw_parent>/canon/）
```

- `<raw_root>` 与 `<canon_root>` 来自 config(`[paths].raw_root` / `[paths].canon_root`),默认同父目录
- 原始文件**只读不改写**;canon 可反复重建（content_hash 变化即重建)
- legacy .md/.txt（现状 `process-raw` 直接读）→ canonicalizer 提供 **legacy 适配器**: 读 .md/.txt,按 heading 语法/IMRAD regex 生成同构 canonical JSON（`parser="legacy_md"` / `parser="legacy_txt"`）,再走同一 chunker —— 保证 chunker 只有 canonical 一个入口

### 5.3 生产管线

```
process-document --format auto|pdf|docx|html|jats|md
  → 原始解析（docling / jats_to_md）→ raw 落盘 <raw_root>/<pmid>/
  → canonicalizer（清洗 + heading 规范化 + source locator + warnings）→ <canon_root>/<pmid>/
  → <pmid>.canon.json 落盘
process-raw --pmid <pmid>
  → 读 .canon.json（优先）;legacy 时经适配器转 canonical（§5.2.3）
  → chunker 只吃 canonical（单一入口）
  → embed + store（generation 化，见 §6）
```

- `process-document` 替换 `process-pdf`（保留 `--pdf` 别名兼容）
- canonicalizer 规则: 首个 H1 定为根、跳级压缩、空标题剔除、HTML selector 黑名单（nav/script/style/ad）、DOCX 样式名→heading 映射表、**warning 分级**（info/warn/error: error 级直接拒绝入库）
- golden fixtures: 每格式 ≥10 份（阶段 5 验收门槛,标题树准确率 ≥95%,表格/公式保留率 ≥95%,0 silent failure）

---

## 6. 数据层设计（P3/P6: generation + 可观测性）

### 6.1 Schema 演进（v2 → v3）

**迁移总原则**: 全部通过**幂等迁移脚本**执行（SQLite 无 `ADD COLUMN IF NOT EXISTS` → 用 `PRAGMA table_info` 逐列检查,见 §6.4 迁移契约）。229MB 生产库不重建表,只加列。

**documents 表新增列**:
```sql
-- 项: content_hash sha256(raw 文件字节);generation 当前 active 代;parser/chunker 版本
ALTER TABLE documents ADD COLUMN content_hash TEXT;
ALTER TABLE documents ADD COLUMN generation INTEGER DEFAULT 1;
ALTER TABLE documents ADD COLUMN parser_version TEXT;
ALTER TABLE documents ADD COLUMN chunker_version TEXT;
```

**chunks 表新增列**:
```sql
-- chunks 是普通表,可 ALTER（幂等检查后执行）
ALTER TABLE chunks ADD COLUMN generation INTEGER DEFAULT 1;
-- generation 同时写入 chunk_id 前缀（见下方编码规范），列仅作冗余索引/查询过滤
```

**chunk_id 编码规范（稳定、可解析、无歧义）**:
```
格式:  <pmid>__g<gen>__<escaped_path>__<part>
示例:  33875643__g2__1_1__p1s1        （V2 旧格式 33875643__1__1.1__p1 仍需兼容解析）
转义:  pmid/path 中的 "__"、"/"、"::" 分别转义为 "_u_"、"_s_"、"_c_"（path 的 "/" 用 "_" 替代,与旧 V2 风格一致）
规则:  gen 恒为整数;part 保留旧 chunker 的 p<N>s<N> 后缀;新旧格式可互解析（解码器优先匹配 g<gen>）
```

**chunk_vectors(vec0 虚拟表)——不 ALTER,generation 走 chunk_id 前缀**:
```sql
-- vec0 是虚拟表,普通 ALTER 不可行 → generation 完全由 chunk_id 前缀承载
-- 查询强制过滤: WHERE chunk_id LIKE '<pmid>__g<active_gen>__%'
-- 重建/校验脚本: corpus rebuild-vectors（读 chunks(active gen) 重嵌,失败可重跑,幂等）
```

**新增 generation_manifest 表（generation 状态机真源）**:
```sql
CREATE TABLE IF NOT EXISTS generation_manifest (
  pmid TEXT NOT NULL,
  generation INTEGER NOT NULL,
  content_hash TEXT,
  parser_version TEXT, chunker_version TEXT, embedder_version TEXT,
  chunk_count INTEGER,
  status TEXT NOT NULL DEFAULT 'building',  -- building|active|retired|failed
  created_at TEXT, activated_at TEXT, retired_at TEXT,
  PRIMARY KEY (pmid, generation)
);
-- documents.generation = 当前 active 的 generation（外键指向 manifest 的 active 行语义）
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
  generation INTEGER,
  parser_version TEXT, chunker_version TEXT, embedder_version TEXT,
  created_at TEXT, updated_at TEXT
);
```

**quality_evidence 表——主键改为 (pmid, project_slug, version_id)（P0: 允许 v1/v2 共存）**:
```sql
-- 原主键 pmid 单列 → 无法存 2 个公式版本。改为: v3 重建表（数据量小,可迁移）:
CREATE TABLE quality_evidence_v3 (
  version_id INTEGER PRIMARY KEY AUTOINCREMENT,   -- 评分版本行（append-only 历史）
  pmid TEXT NOT NULL,
  project_slug TEXT NOT NULL,
  formula_version TEXT NOT NULL,                  -- 'v1' | 'v2'（公式版本,与数字 version_id 区分）
  rubric_version TEXT,
  prior_weight REAL, evidence_weight REAL,
  criterion_validity REAL, outcome_reliability REAL, conclusion_data_consistency REAL,
  evidence_mean REAL, quality_final REAL,
  quality_status TEXT DEFAULT 'evidence',         -- evidence | partial | prior_only | override
  scorer_id TEXT, notes TEXT,
  scored_at TEXT
);
CREATE INDEX idx_quality_evidence_v3_current ON quality_evidence_v3(pmid, project_slug, scored_at DESC);
-- current 指针 = 每个 (pmid, project_slug) 的最新 scored_at 行;旧 v1 行保留（append-only）
-- 旧表迁移: INSERT INTO v3 SELECT ... formula_version='v1', scored_at=computed_at FROM quality_evidence
```

**relevance 表新增列（陈旧传播）**:
```sql
ALTER TABLE relevance ADD COLUMN generation INTEGER DEFAULT 1;
ALTER TABLE relevance ADD COLUMN stale INTEGER DEFAULT 0;      -- 0 可用 / 1 陈旧（重切片或 query 策略变后）
ALTER TABLE relevance ADD COLUMN query_strategy TEXT;          -- 关联 embedding/rerank 版本指纹
```

### 6.2 写入事务语义

- vec0 表 `INSERT OR REPLACE` 无效 → **必须 DELETE + INSERT（现状 db.py 219/227/264/274 行仍在用 INSERT OR REPLACE —— 阶段 4 必须统一改为 DELETE+INSERT,这是 codex 抓出的源码矛盾）**
- 写操作: 单 writer（worker 内互斥锁 / CLI 单进程），连接 `PRAGMA busy_timeout=30000`
- 事务边界: chunks + chunk_vectors + generation_manifest 状态 + ingestion_jobs 状态 **同一事务提交**;**embedding API 调用在事务外完成**（网络 IO 不得占事务）,事务内只写已算好的向量
- 失败恢复: 事务回滚 → 新代不残留;embedding 失败 → job 留 `stage=embedded, status=failed` 可重试，**不得**留下"已索引"虚状态

### 6.3 迁移契约（SQL migration contract）

- `PRAGMA user_version` 记录 schema 版本（当前 2 → 目标 3）;迁移脚本按版本链逐个执行（2→3）
- 每条 DDL 前用 `PRAGMA table_info(<t>)` 检查列存在性（幂等）;`CREATE TABLE IF NOT EXISTS` 用于新表
- vec0: 不迁移 schema,只做**一致性校验**（chunk_id 维数、count 比对 chunks↔vec0）;重建走 `rebuild-vectors`
- 迁移前检查: `integrity_check`、磁盘 ≥2× 库大小、WAL checkpoint;迁移中 `busy_timeout`;迁移后 `fsck`
- **229MB 生产库备份**: 不用 `cp`（在线不一致）→ 用 SQLite backup API（`VACUUM INTO` 或 Python `sqlite3` backup）生成一致快照,验证 `integrity_check` + 行数后归档
- 启动自检失败（schema 版本不符 / vec0 缺 / 维度错）→ **拒绝写入,不静默降级**

---

## 7. 在线召回: Python Worker + IPC（P5）

### 7.1 选型结论（采纳 Codex 审查）

- **A 常驻 Python worker + Unix socket JSONL**: 首选（消灭 spawn 冷启动、成熟 sqlite-vec 生态、无 native Node ABI 风险）
- **C spawn 降级**: 保留为 feature flag（`CORPUS_QUERY_MODE=spawn`），worker 不可用时自动降级
- **B better-sqlite3**: 否决（native 编译/ABI/安装迁移风险，node:sqlite loadExtension 被禁已堵死纯 TS 直连）

### 7.2 协议（JSONL over Unix socket）

**帧格式**: 每行一个 JSON 对象,`\n` 分隔;单行上限 **1MB**（超限 worker 直接断连,防半包放大）;`schema_version` 首字段必需,不匹配拒绝。

请求（每行一个 JSON）:
```json
{"schema_version": 1, "request_id": "u1-1725", "cmd": "query", "params": {"project": "ar-review", "query": "EGFR", "top_k": 10}}
```

响应:
```json
{"schema_version": 1, "request_id": "u1-1725", "ok": true, "result": {...}}
{"schema_version": 1, "request_id": "u1-1725", "ok": false, "error": {"code": 3, "message": "db unavailable"}}
```

**幂等与去重（重要: 内存去重不够,需持久化）**:
- 持久化幂等表 `ipc_idempotency(request_id PK, cmd, status, result_sha, created_at, expires_at)`;
  worker 重启后幂等窗口内（默认 24h）重复 request_id 直接返回缓存结果（查询）或"已处理"（写）
- `score` 写操作另用业务幂等键: `(pmid, project_slug, scorer_id, scored_at)` —— 超时后客户端**查询状态而非盲重试**（`score-status` 命令或幂等表查询）
- 查询幂等: 天然幂等,重试安全;写操作: 未收到响应 → 查幂等表决定重试或放弃

**socket 安全与单实例**:
- socket 目录 0700 / 权限 0600;启动时清理 stale socket（pid 文件 + `flock` 单实例锁,防多 worker 竞争）
- worker 启动自检失败 → 删除自己 pidfile 退出,插件侧感知降级 spawn

命令集（对齐 5 个工具 + 运维）:
| cmd | 对应工具 | 说明 |
|---|---|---|
| `query` | corpus_query | 召回+评分+推荐（含 rerank）|
| `search_self` | corpus_search_self | 自写文档检索 |
| `score` | corpus_score | 写评分 + 回算 quality_final（JSON 响应，**替代正则解析**）|
| `score-status` | — | 幂等查询: 某 score 是否已提交（超时恢复用）|
| `get_chunk` | corpus_get_chunk | 树上下文 |
| `similar_level` | corpus_list_similar_level | 同层级平行 section |
| `health` | — | 健康检查（worker 存活 + vec0 加载自检）|
| `fsck` | — | 一致性校验（§8.2）|

错误码: 0 ok / 1 not_found / 2 invalid_args / 3 db_error / 4 embedding_error / 5 timeout / 6 worker_busy / 7 schema_mismatch / 8 unknown_cmd

### 7.3 Worker 生命周期

- 启动: `corpus worker --socket <config.paths.worker_socket>`（daemon 由插件或 a2a_wake 拉起,pidfile + flock 单实例）
- 空闲退出: 300s 无请求自退（防僵尸,退出前清 pidfile）;插件检测 socket 失效 → spawn 降级 → 空闲重试拉起（**拉起竞态由 flock 单实例锁消除**）
- 超时: 请求 30s;health 10s;embedding/rerank 调用 60s
- 幂等: 持久化幂等表覆盖进程重启（§7.2）;查询天然幂等;写操作超时 → 查 score-status 决定重试
- 并发: 读并发（SQLite WAL）;写互斥（单 writer,显式写锁,不依赖 SQLite 隐式锁）
- 启动自检: vec0 加载、维度、距离度量、schema 版本 → 写诊断日志;失败拒绝服务并输出错误码

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
  │   ├─ generation+1 → generation_manifest 插行 status=building
  │   ├─ 写新 chunks（chunk_id 带 g<new> 前缀）+ 新向量（DELETE+INSERT）
  │   ├─ 校验: chunks↔vec0 一一对应（count 比对）+ manifest.chunk_count 写入
  │   ├─ 单事务原子切换: documents.generation=new + manifest.status=active（指针+manifest 同事务）
  │   ├─ 旧代保留（回滚窗口,默认 7 天）→ status=retired 后异步清理 chunks/vec
  │   └─ 失败任一步 → 事务回滚 + 新代全量删除 + manifest.status=failed（指针不动,旧代继续 active）
  └─ relevance: 旧 generation 相关行标 stale=1（查询条件: relevance.generation = documents.generation AND stale=0）
```

**回滚窗口**: 旧代数据保留 7 天（config `[paths].retention_days`），期间 `fsck --restore <pmid>` 可回退指针到旧代;窗口期满异步 purge。

### 8.2 fsck 命令

- chunks ↔ chunk_vectors 一一对应（查缺失/重复/orphan,按 active generation）
- chunk_id 维度校验（embedding 维度 = config.embedding.dim）
- 树完整性: parent_id/child_ids/sibling_ids 交叉引用无悬空
- 评分一致性: quality_evidence_v3 current 指针唯一性
- 输出报告 + `--fix` 选项（孤儿向量清理,幂等）

### 8.3 陈旧传播规则

| 变更 | 影响 | 处理 |
|---|---|---|
| documents.article_type/prior 修改 | quality_evidence_v3 | 新评分行 append（formula 不变,prior 变）;当前指针指向最新行 |
| 重切片（generation+1） | relevance / chunks-vec | relevance 标 stale=1;查询强制 active generation（chunk_id LIKE + documents.generation）|
| embedder 版本变更 | 全部向量 | 全量重嵌（redo: 新 generation,旧代 7 天回滚窗口,期间旧代可读）|
| query 策略变化（rerank/权重） | relevance | query_strategy 指纹变化 → 相关行 stale=1,下次查询重算 |

---

## 9. 评分系统（P4: formula_version）

### 9.1 公式版本（append-only 共存，不覆盖）

| 版本 | 公式 | 状态 |
|---|---|---|
| v1（现状） | `quality_final = max(prior_num, evidence_mean)` | 冻结（历史行保留）|
| v2（新，拍板） | `quality_final = 0.7×evidence_mean + 0.3×prior_num`; 无打分 → `prior_num` + `quality_status=prior_only` | 阶段 4 实现 |

**存储模型（已拍板: 不可变历史表 + current 指针,不原地更新）**:
- `quality_evidence_v3` 是 append-only 历史（§6.1）;每次 scoring 写入 = 新行（version_id 自增）
- current 指针 = (pmid, project_slug) 下 `scored_at` 最新行;旧 v1 行**永不被修改**,全部保留
- `formula_version`（v1/v2 公式语义）与 `version_id`（行号）两个维度分离,可追溯任何历史状态

**状态枚举（统一,代码/API/schema 三处一致）**:
```
evidence     — 3 特征全打分（curator 确认）
partial      — 特征数 1-2（证据不全,默认不进 primary）
prior_only   — 无 curator 打分,仅有类型先验（冷启动,绝不伪装已审核）
override     — 人工强制指定等级（存 override_reason + override_by,可审计）
```

### 9.2 recompute-scores（迁移命令）

```
corpus recompute-scores --formula v2 [--project <slug>] [--create-prior-only]
  → 只处理有 evidence 的行（evidence_n≥1）: 按 formula_version=v2 重算 → append 新行（旧行保留）
  → --create-prior-only: 对无 evidence 的 documents 生成 prior_only 行（默认不生成,避免噪音）
  → 幂等: 以 (pmid, project_slug, formula_version, rubric_version) 判重,重复执行不产生重复行
  → 失败重试: 按 (pmid, project_slug) 批次,失败批次记录可重跑,不半途
  → 审计: 每行写 scorer_id="system:recompute", notes 记录触发原因
```

### 9.3 推荐阈值（策略层,可配置,不写死）

- **现状** `make_recommendation`（db.py 436 行）阈值硬编码: primary ≥0.55 & ≥0.75; supporting ≥0.50 —— **阶段 4 改为从 config `[scoring].thresholds` 读取**
- v2 公式改变分布 → 阈值不再声称"不变",初始沿用旧值,阶段 5 用标注集 PR 曲线校准后写回 config
- **quality_status 对推荐的影响（实现位置: 推荐策略层,不污染数据层）**:
  - `prior_only` / `partial`: **默认不得进入 primary**（除非显式 override;override 行 quality_status=override 放行）
  - 同分排序: `evidence > partial > override > prior_only`（已审优先,P7）
- 推荐是**策略层**概念（query 时计算）,不落库;阈值/状态规则全部可配置可审计

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
| M1 | config 层（corpus.toml + config.py）+ 6 处硬编码消除 + **SQL 迁移契约骨架（§6.3）** | v0.2.0 |
| M2 | canonical schema + canonicalizer + legacy md/txt 适配器 + process-document 多格式入口 + golden fixtures 初版 | v0.2.0 |
| M3 | worker + IPC 状态机（含幂等表 + socket 单实例）+ TS worker-client + spawn 降级 | v0.3.0 |
| M4 | generation_manifest/ingestion_jobs/fsck + re-chunk 状态机 + **vec0 写入统一 DELETE+INSERT（P10）** | v0.3.0 |
| M5 | 评分 v2（quality_evidence_v3 append-only）+ recompute-scores + prior_only/partial/override | v0.4.0 |
| M6 | 测试补齐（db/cli/worker/canonicalizer/迁移契约）+ 单测 | v0.4.0 |
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