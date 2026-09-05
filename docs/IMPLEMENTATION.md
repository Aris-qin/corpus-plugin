# IMPLEMENTATION.md — 实现级契约 v1.0

状态：**实现级契约 v1.0，阶段 4 唯一依据**（2026-09-06）。本文件把复审报告的五项放行条件冻结为可编码、可迁移、可测试的契约。若源码与本文件冲突，以本文件为准；不得在实现阶段自行改变字段、ID、状态枚举或兼容边界。

与 `ARCHITECTURE.md` 的关系：本文件取代并细化其 §5 canonical schema、§6 generation/迁移、评分设计、§7 IPC 和 `MIGRATION.md` 的备份/回滚步骤；产品层面的工具名称、参数语义和推荐策略仍以 `PRODUCT.md` 为准。本文件只新增 `IMPLEMENTATION.md`，不要求修改上述文档。

## 1. Canonical 冻结契约

### 目标
所有输入格式先变成唯一的 `.canon.json`，chunker 只接受 canonical 文档；节点可追溯到原文，重复解析在内容不变时产生相同 ID。legacy Markdown/TXT 通过适配器进入同一入口。

### 契约
文档对象必须为：
```json
{"schema_version":"1.0","doc_id":"string","content_hash":"sha256:<64hex>","source_file":"repo-relative/path","parser":"string","parser_version":"string","nodes":[Node],"parser_warnings":[{"level":"info|warn|error","code":"string","msg":"string"}]}
```
Node 必须有：`node_id,kind,level,sec_num,title,text,ordinal,parent_id,path,locator,attrs`。kind 枚举为 `heading|paragraph|table|formula|caption|list|footnote|inline_formula|figure`；level 为 heading 的 1..6、其他为 0；非 heading 的 title 为空；ordinal 是显示顺序；path 是 sibling 序号路径（根 `/1`）；locator 使用半开 `char_start/char_end`，未知为 null。

`text` 是唯一下游文本字段：heading=去标记折叠空白的标题；paragraph=规范化段落；table=完整 GFM Markdown；formula/inline_formula=canonical LaTeX；caption=caption 纯文本；list=每项一行 `- ` 文本；footnote=脚注全文；figure=alt 文本，无 alt 为 `[figure]` 并写 warn。原始 markdown/latex/alt 保存在 attrs，不能只填它们而遗漏 text。table attrs 至少含 markdown，formula attrs 含 latex/display，figure attrs 含 alt/uri，caption/list/footnote 含关联节点字段。

node_id 为 `nd_` + sha256(utf8(`content_hash\0canonical_path\0kind\0text_normalized`)) 前 12 hex；canonical_path 由父 path 和同级 kind/text 出现计数生成，ordinal/locator 不参与。若截断碰撞，扩展到 16/20 hex 并写 node_id_full；同 ID 不同 full hash 必须拒绝。parent 必须存在或 null，path 与 parent 链一致，ordinal 严格递增；跳级 heading 压缩；error warning 拒绝入库。

签名：
```python
def canonicalize(source: bytes, *, source_file: str, fmt: str = "auto",
                 parser_version: str, doc_id: str | None = None) -> dict: ...
def canonicalize_file(path: Path, *, output: Path, fmt: str = "auto") -> CanonicalDocument: ...
def adapt_markdown(text: str, *, source_file: str, content_hash: str) -> dict: ...
def adapt_txt(text: str, *, source_file: str, content_hash: str) -> dict: ...
def process_raw(pmid, *, canon_path=None, legacy_path=None, generation=None) -> ProcessResult: ...
```
canonical 文件写入 `<canon_root>/<pmid>/<content_hash>.canon.json`，临时文件后 `os.replace`。process-raw 优先 canon；无 canon 才调用 adapter，不直接调用旧 markdown chunker。golden fixtures：`tests/fixtures/canonical/{markdown,txt,pdf,docx,html,jats}/{case}.input` 与同名 `.canon.json`，legacy 在 `tests/fixtures/canonical/legacy/{md,txt}/`，每格式至少 10 组。

### 改哪些文件哪些函数
新增 `corpus/canonical.py`；改 `corpus/chunker_markdown.py` 增加 `chunk_canonical(doc)`；改 `corpus/cli.py` 的 `cmd_process_raw/cmd_process_document`；chunk_query 只读 chunks。

### 测试用例清单
断言重复标题、前置插入、重跑时 node_id 稳定；每 kind 的 text 规则；parent/path/ordinal 不变量；非法 parent/locator/error 被拒；golden 规范化 JSON 相等；process-raw 缺 canon 时调用 adapter 且不直调旧 chunker。

## 2. chunk_id、generation 与 vec0 过滤

### 目标
ID 可逆、无碰撞，旧 ID 可读；manifest 是 hash/chunker 版本的唯一绑定；所有 KNN 强制 active generation。

### 契约
新 ID：`c2.<gen8>.<hash12>.<chunker8>.<n><len>:<path><len>:<part>`，例如 `c2.g000001a.ab12cd34ef56.ck000001.2:5:1/2:3:p1`。长度前缀使 `/`,`_`,`__` 无特殊含义。gen8 为 base36，hash12=content_hash 前 12 hex，chunker8=sha256(name+version) 前 8。旧 `pmid__path__pN` 返回 legacy=true、generation=null，只允许兼容查询。
```python
NEW = re.compile(r'^c2\\.([0-9a-z]{8})\\.([0-9a-f]{12})\\.([0-9a-f]{8})\\.(\\d+):')
def parse_chunk_id(s):
    if s.startswith("c2."):
        fields = read_length_prefixed_fields(s); require_end(s); return ChunkId(*fields, legacy=False)
    if LEGACY_RE.fullmatch(s): return ChunkId(raw=s, legacy=True)
    raise InvalidChunkId
```

DDL：
```sql
CREATE TABLE generation_manifest(
 generation INTEGER PRIMARY KEY, pmid TEXT NOT NULL, content_hash TEXT NOT NULL,
 chunker_name TEXT NOT NULL, chunker_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('building','active','retired','failed')),
 created_at TEXT NOT NULL, activated_at TEXT, retired_at TEXT,
 chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE UNIQUE INDEX generation_one_active_per_pmid
 ON generation_manifest(pmid) WHERE status='active';
ALTER TABLE documents ADD COLUMN generation INTEGER REFERENCES generation_manifest(generation);
CREATE INDEX documents_generation_idx ON documents(pmid,generation);
```
构建在 BEGIN IMMEDIATE 创建 building、写 chunks/vectors；切换事务内旧 active→retired、新→active、documents.generation 更新。失败回滚并标 failed；崩溃启动清理无引用 building；旧代保留 7 天。迁移前清理孤儿并开启 foreign_keys，NULL 仅保留 legacy。

`CorpusDB.vector_search(query_vector, *, project=None, pmid_filter=None, generation=None, top_k=10)`：未传 generation 时先得到每个 pmid 的 active generation，再进入 vec0 MATCH；显式 generation 只能 active（离线审计另开 include_retired）。SQL 必须是 vec0 MATCH 后立即 JOIN `chunks c ON c.chunk_id=v.chunk_id` 和 `generation_manifest g ON g.generation=c.generation AND g.status='active'`，然后 project/pmid 过滤排序。chunk_query 的 get/similar 复用 helper，不能 Python 过滤 KNN。旧 ID 仅在兼容窗口映射，找不到返回 410，不允许写入。

### 改哪些文件哪些函数
改 `corpus/db.py` 的 vector_search/chunk insert/delete/generation transaction；`corpus/cli.py` process-raw/rechunk；`plugins/corpus-query/chunk_query.py` get_chunk_meta/cmd_similar_level；新增 `corpus/generation.py`。

### 测试用例清单
路径含特殊下划线的 round-trip/碰撞；旧 ID 解析拒写；两个 active 被唯一索引拒绝；切换崩溃保持旧 active；失败代无孤儿；KNN 不返回 retired；229MB fixture 行数、active 唯一性和 orphan 检查。

## 3. 评分 v3 全链路契约

### 目标
评分历史 append-only、current 确定、override 可审计；v1/v2 共存，重算可重试不重复。

### 契约
```sql
CREATE TABLE quality_evidence_v3(
 evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
 pmid TEXT NOT NULL, project TEXT NOT NULL,
 formula_version TEXT NOT NULL, rubric_version TEXT NOT NULL,
 criterion_validity REAL, outcome_reliability REAL, conclusion_data_consistency REAL,
 evidence_mean REAL, prior_score REAL, quality_final REAL NOT NULL,
 quality_status TEXT NOT NULL CHECK(quality_status IN ('prior_only','partial','scored','override')),
 override_reason TEXT, override_by TEXT,
 source TEXT NOT NULL, source_event_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
 scored_at TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(pmid) REFERENCES documents(pmid)
);
CREATE INDEX quality_v3_lookup ON quality_evidence_v3(pmid,project,formula_version,rubric_version,scored_at,evidence_id);
CREATE TABLE quality_current(
 pmid TEXT NOT NULL, project TEXT NOT NULL, formula_version TEXT NOT NULL, rubric_version TEXT NOT NULL,
 evidence_id INTEGER NOT NULL UNIQUE REFERENCES quality_evidence_v3(evidence_id),
 selected_at TEXT NOT NULL, selection_reason TEXT NOT NULL,
 PRIMARY KEY(pmid,project,formula_version,rubric_version)
);
```
current 在同一事务按 `scored_at DESC, evidence_id DESC` 选取并显式 upsert，绝不运行时只排序。idempotency_key=sha256(`pmid|project|formula|rubric|canonical_input_json|override_by|override_reason`)，不含 scored_at；同 key 返回原行，不新增。

旧表迁移：BEGIN IMMEDIATE 创建 v3/current/map；映射 prior→prior_score、evidence_mean、quality_final，缺列 NULL；status 按 evidence/prior 映射 prior_only/partial/scored；重复按业务输入保留 scored_at 最大、旧 pk 最大，其余记 map；外键先校验 documents；每批提交 marker。失败整事务回滚，成功后 rename 为 `quality_evidence_legacy_backup_<timestamp>`，保留 30 天。重跑从 marker 继续；foreign_key_check/行数失败恢复快照。

改造清单：`corpus/db.py` 新增 insert_quality_v3/get_current_quality/upsert_quality_current/recompute_idempotency_key，删除旧 INSERT OR REPLACE；`corpus/cli.py` cmd_score/cmd_recompute_scores 输出 JSON（含 formula/rubric/status/evidence_id/idempotency_key），文本只 --legacy-text；worker score/score-status 调 DB API；TS index.ts corpus_score 改 JSON schema 校验，不正则文本；query/recommendation 读 quality_current，prior_only/partial 默认不进 primary，override 返回审计字段。

### 测试用例清单
并列 scored_at 时 evidence_id 大者 current；v1/v2 共存；同 key 重试正文相同且行数不变；override 缺 reason/by 被拒；迁移去重确定；checkpoint 异常可恢复；TS 返回 status/formula/evidence_id；推荐过滤 prior_only/partial。

## 4. Backup/restore runbook

### 目标
SQLite backup API 快照一致；恢复没有 writer、残留 WAL/SHM 或未验证库。

### 可执行备份脚本
```python
#!/usr/bin/env python3
from datetime import datetime, timezone
from pathlib import Path
import sqlite3, sys
src_path = Path(sys.argv[1]).resolve()
backup_dir = Path(sys.argv[2]).resolve(); backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
dst_path = backup_dir / f"{src_path.stem}.bak-{stamp}.sqlite"
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(str(dst_path))
try:
    src.execute("PRAGMA wal_checkpoint(PASSIVE)")
    src.backup(dst, pages=4096, sleep=0.1); dst.commit()
    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise RuntimeError("backup integrity_check failed")
finally:
    dst.close(); src.close()
print(dst_path)
```
恢复：停止 worker、CLI、定时任务和所有 writer；确认 lsof 无写者；checkpoint(TRUNCATE) 后关闭连接；将当前 db/wal/shm 移到带时间戳 quarantine（不删除），复制快照为 db；同名 WAL/SHM 一并恢复，否则确保不存在；新连接开启 foreign_keys，运行 integrity_check、foreign_key_check、vec0 fsck（chunks/vec0 行数和 ID 集合）后才启动 worker。恢复失败保留 quarantine，回到上个快照。

中断/重跑：目标仅在 integrity OK 后可用；已存在目标先校验不覆盖。磁盘不足捕获 sqlite3.Error/OSError，删除未通过校验的临时目标并报告余量；迁移事务回滚、服务只读。229MB fixture 要求快照可开、三项校验通过、并发读不丢行、SIGTERM/配额/WAL 中断可恢复，恢复后 chunks/vec0/quality_current 与基线一致。

### 改哪些文件哪些函数
新增 `corpus/backup.py`；改 `corpus/cli.py` 增加 backup/restore-check；worker health gate 在 integrity/fsck 失败时拒写。

### 测试用例清单
文件名不含字面 `$(date)`；并发 reader 快照可开；SIGTERM/磁盘满重跑；WAL/SHM quarantine；完整性失败不启动 writer；229MB fixture 全通过。

## 5. IPC 幂等与 worker 状态机

### 目标
半包、超时、重启和降级不重复评分；重复请求直接返回完整正文；stale socket 自动恢复。

### 契约
```sql
CREATE TABLE ipc_idempotency(
 idempotency_key TEXT PRIMARY KEY, request_id TEXT NOT NULL, command TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, response_json TEXT, response_sha256 TEXT,
 status TEXT NOT NULL CHECK(status IN ('processing','committed','failed')),
 error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
```
协议每行 JSON，schema_version=1、request_id 全局唯一、最大 1 MiB；reader 支持任意半包，换行后才 dispatch。score 业务键是客户端 score_event_id（无则 TS 首次生成并重试复用），数据库键 `score:project:pmid:formula:rubric:score_event_id`，绝不含 scored_at。worker 先写 processing；committed 直接返回 response_json 并校验 request hash；processing 超过 lease 30s 才能同 key 接管；failed 返回缓存错误。

状态机：STOPPED→STARTING→READY；连接失败清理 stale socket（确认 pid 不存活）并一次 spawn；READY→DEGRADED（超时/断连），查询可 spawn，写请求先 score-status；spawn 失败 BACKOFF（1/2/4 秒，最多 3 次）后返回 unavailable；健康后回 READY，优先常驻 worker。pidfile+flock 单实例，socket 0600。

### 改哪些文件哪些函数
新增 `corpus/ipc.py` frame reader/writer/cache；新增/改 `corpus/worker.py` dispatch/lease/health；改 TS `index.ts` client/fallback/score_event_id/JSON response；chunk_query 使用同一配置，不硬编码 vec 路径。

### 测试用例清单
半包只 dispatch 一次；>1MiB 返回 frame_too_large；响应丢失重试返回完全相同正文且 DB 一行；重复 score_event_id 不双写；stale socket 仅死 pid 可清理；worker 崩溃后查询降级、写先 status；三次 backoff 后错误可观测；恢复后常驻优先；未知 command/schema 结构化错误；缓存过期才执行。

## 6. 阶段 4 完成门
每个测试清单必须有自动化测试并通过，229MB fixture 验收完成，生产库未写入前才算完成；契约变更必须更新版本号并重新审查。

