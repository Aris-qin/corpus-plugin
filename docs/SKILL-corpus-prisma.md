---
name: "corpus-prisma-research-pipeline"
description: "PRISMA 7 阶段 + 项目内 SQLite+sqlite-vec corpus 做科研内容管理。researcher 入库,curator 评分出 3 维分级 recommendation。"
---

# Corpus + PRISMA Research Pipeline

> 项目内 SQLite + sqlite-vec 向量数据库,PRISMA 7 阶段协作流水线,
> 解决"主 agent 引用文献靠猜和推测"的问题。

---

## 何时用

- 多个科研项目并行(综述、基金、论文),每个项目需要独立的文献管理 + 内容深度学习
- 主 agent 引用文献时需要"轻量摘要 + 可追溯 chunk 引用",不能每次都 fetch 全文
- 文献需要按 **3 维评分**(relevance / quality / venue)排序,不是只看相关度
- 多个子 agent 协作,需要一个共同事实层(corpus DB)解耦
- **跨项目文献共享**:同一篇文献被多个项目引用时,只存一份全文,通过 document_groups 表关联

**不要用于**:
- 简单 web 搜索(用 `web_search` 或 `glm_web_search`)
- 单次一次性查询(用 fetch + 摘要即可)

## V2 补充(2026-07-19):自写文档入库与 RAG 检索

V2 在原 V1(仅外部 PMID 文献)之上扩展了 **自写文档入库 + tree-aware RAG 检索**能力。适用场景:综述 / 论文 / 基金的草稿本身需要入库、检索、作为引证。

### V2 架构(补充原架构图)

```
...
                          ↓ register-raw (researcher) / process-raw (curator) / ingest-doc (curator, self-written)
┌─────────────────────────────────────────────────────────────┐
│ 统一文献库 (SQLite + sqlite-vec)                              │
│   projects/_corpus/corpus.db                                 │
│     documents.source 字段区分:                              │
│       pubmed | glm_web | url_fetch | file_parse |            │
│       self_written  ←  V2 新增                               │
│     chunks 扩展 V2 字段(v2 migration):                    │
│       level / path / parent_id / child_ids / sibling_ids /   │
│       heading_chain / doc_id                                  │
└─────────────────────────────────────────────────────────────┘
                          ↑ ingest-doc (V2)
                          │
┌─────────────────────────────────────────────────────────────┐
│ sci-writing-expert (V2)                                      │
│   - 调 corpus ingest-doc + search-self + answer-self         │
│   - 引用占位新增 [chX > X.Y > X.Y.Z](self_written)         │
└─────────────────────────────────────────────────────────────┘
```

### V2 5 个关键决策

10. **heading-aware chunking** - 每个 heading 是一个独立 chunk 节点;超过 400 字按句号切(p1...pN)。HR 与 blockquote 不作切片信号,收集为正文。
11. **tree metadata 存在 chunks 表** - V2 migration 增加 7 列 + 3 个索引;不创建新表,避免破坏现有 corpus-curator / query 逻辑。
12. **self_written pmid 命名**:`selfw__<doc_slug>__<basename>` 例:`selfw__review_ai_fall_elderly__draft_ch5`。 documents.source = 'self_written'。
13. **tree rerank** - vector_sim + path-prefix boost(同 path 多个命中 → 每条 +0.05 × 命中数);可选 --level 过滤 / --expand-context 拉 parent+siblings。
14. **混合检索边界** - search-self 只查 self_written;answer-self 默认 LLM 为 deepseek-v4-flash(openclaw.json 已有);不含 LLM 编排,不入引用输出(独立 CLI)。

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ 主 agent 阿呆                                               │
│   - 不读全文                                                 │
│   - **可直接调 corpus-query-tool plugin tool**(2026-08-16 开放) │
│     - corpus_query / corpus_search_self / corpus_get_chunk / │
│       corpus_list_similar_level / corpus_score               │
│   - 只读 curator 的 result.json(轻量)+ corpus_ref(指针)  │
│   - 写操作(ingest / register-raw)仍走 sessions_spawn → researcher │
│   - 派子 agent 一律用 sessions_spawn tool(first-class)     │
└─────────────────────────────────────────────────────────────┘
                          ↑ curator result.json
                          │
┌─────────────────────────────────────────────────────────────┐
│ corpus-curator (PRISMA Stage 6-7)                            │
│   - 读 corpus DB (按 corpus_ref)                              │
│   - 重算 evidence features + recommendation                  │
│   - 抽 key_findings / quote / evidence_locations            │
│   - 调 `corpus process-raw` (切片入库) + `corpus query` + `corpus score` CLI │
└─────────────────────────────────────────────────────────────┘
                          ↑ corpus DB
                          │
┌─────────────────────────────────────────────────────────────┐
│ 统一文献库 (SQLite + sqlite-vec)                              │
│   projects/_corpus/                                          │
│     corpus.db          8 表 + vec (documents/chunks/...)    │
│     raw/<pmid>.txt     全文缓存(所有项目共享)               │
│     document_groups    项目分组(一篇文献可挂多个项目)       │
└─────────────────────────────────────────────────────────────┘
                          ↑ ingest
                          │
┌─────────────────────────────────────────────────────────────┐
│ researcher (PRISMA Stage 2-5)                                │
│   - 多源检索 + 抓全文 + 切片 + 入库 + 3 维初始评分            │
│   - 调 `corpus init` + `corpus register-raw` CLI               │
└─────────────────────────────────────────────────────────────┘
```

**关键设计**:
- **统一总库**:所有项目共享一个 `projects/_corpus/corpus.db`
- **项目分组**:通过 `document_groups` 表实现,一篇文献可挂多个项目(role: core/peripheral/cited)
- **全文共享**:`raw/<pmid>.txt` 只存一份,所有项目共享
- **CLI 命令**:`init/register-raw/process-raw/ingest-doc/query/search-self/answer-self/score/group/list-projects/list-groups`

---

## PRISMA 7 阶段映射

| 阶段 | 角色 | 工具 | 输出 |
|---|---|---|---|
| 1. Protocol | 主 agent 阿呆 | - | 建 corpus,定义 schema |
| 2. Search strategy | researcher | PubMed EDirect, GLM web_search | candidates[] |
| 3. Screening | researcher | 关键词命中率 + 去重 | candidates_for_ingest |
| 4. Eligibility | researcher | efetch, web_fetch, docling | raw/<pmid>.txt |
| 4.5 Pre-ingest check | researcher | `corpus register-raw --verify-only` | title 关键词 + abstract 重合率 |
| 5. Inclusion | researcher | `corpus register-raw` | documents 元数据 + raw 落盘 + venue |
| 5.5 Processing | corpus-curator | `corpus process-raw` | chunks + chunk_vectors (V2 heading-aware) |
| 6. Data extraction | corpus-curator | `corpus query` + `corpus score` | quality_evidence 重算 |
| 7. Synthesis | corpus-curator | aggregate + recommendation | articles[] in result.json |
| **7.5 Plugin 直查** | **任何 agent** | `corpus_query` / `corpus_get_chunk` / `corpus_list_similar_level`(plugin tool) | **直接 tool 结果,不走 subagent** |

---

## 3 维评分(横向分级)

**关键** -- **不要只看相关性**。Zotero/Rayyan 的核心理念:相关性 + 质量 + 期刊 venue 三维度。

| 维度 | 数据来源 | 评分 | 用途 |
|---|---|---|---|
| **relevance** | chunk 向量相似度 + 关键词命中 | 0-1 连续 | 检索排序 |
| **quality** | article_type_prior + evidence features | 离散档位 | 引用过滤 |
| **venue (IF)** | OpenAlex 2yr_mean_citedness | 浮点 / 未知 | 同分排序 |

### Recommendation 阈值(写在 db.py)

```
primary:     quality ≥ medium (0.55) AND relevance ≥ 0.75
supporting:  quality ≥ medium (0.55) AND relevance ≥ 0.50
background:  其它有相关性的文献
```

**关键不变量**:
- venue **不惩罚**低 IF / unknown IF -- 仅在同分时排序
- type_prior=unknown 时 evidence_mean 主导 quality (max 策略)

---

## 8 步实施(已完成的 SOP)

### Step 1: corpus 基础设施 ✅
- 路径:`scripts/corpus/{db.py, cli.py, openalex.py}`
- 5 表 schema + vector search + 3 维评分 + recommendation + snippet 提取
- CLI 命令:init / register-raw / process-raw / ingest-doc / query / search-self / answer-self / score / group / list-projects / list-groups
- 端到端验证:拿 PMID 33875643 跑完整 init → ingest → score → query → primary

### Step 2: researcher SOUL 重写 ✅
- 路径:`agents/researcher/SOUL.md` (v2, 319 行)
- PRISMA Stage 2-5 工作流
- result.json v2 schema:corpus_ref / ingested_chunks / article_type / quality_type_prior / venue_if
- 与 curator 边界严格互斥(researcher "我做" = curator "不做")

### Step 3: corpus-curator 新建 ✅
- 路径:`agents/corpus-curator/SOUL.md` (v1, 424 行)
- PRISMA Stage 6-7 工作流
- 重算 evidence features (3 项 prompt 完整 + 评分锚点)
- result.json schema:articles[] + key_findings / quote_candidates / evidence_locations / relevant_chunks
- 不直连 metadata.sqlite,所有 DB 操作走 CLI

### Step 4: SCHEMA.md 更新 ✅
- 路径:`agents/SCHEMA.md` (297 行)
- researcher v2 + corpus-curator v1 schema 完整字段表
- 互斥边界表 + v1→v2 升级说明

### Step 5: ar-review pilot dry-run 验证 ✅
- 3 篇按质量档抽样(meta-analysis / cohort / narrative review)
- 阿呆手动跑 corpus CLI 完整 init → ingest → score → query
- recommendation 跟预期完全一致:P1/P2 primary,P3 background
- 5 张表 schema 全填充:3 docs / 3 venue / 15 chunks / 15 vectors / 3 quality_evidence

### Step 6: agent 注册到 OpenClaw 系统 ✅
- 关键教训:写 SOUL.md ≠ 注册完成。OpenClaw 有 2 层独立的注册路径:

**注册 checklist(5 件必做)**:
- [ ] `agents/<agent-name>/SOUL.md`(行为规则)
- [ ] `agents/<agent-name>/config.json`(model / capabilities / tools)
- [ ] `agents/master.md` 团队表加一行
- [ ] `agents/master.md` 技能分配表加一行
- [ ] `openclaw.json: agents.list[]` 加 entry(**最关键的一层**)

**验证方式**:
```bash
openclaw agents list 2>&1 | grep <agent-name>
# 必须出现才算注册成功
```

### Step 7: agent model 选择 ✅
- corpus-curator 原本 zhipu/GLM-5.1 → 撞限频 → 切 minimax/MiniMax-M3
- fallback 保留 deepseek/deepseek-v4-pro(历史合理兜底)
- 改 2 处:`openclaw.json` + `agents/<name>/config.json`
- gateway restart 后 `openclaw agents list` 验证 Model 字段

### Step 8: 端到端真实任务验证(sessions_spawn tool)✅
- 阿呆(主 agent)在对话里直接 `sessions_spawn(task=..., taskName=..., runtime="subagent")`
- 验证 corpus-curator 通过 sessions_spawn 真实跑 ar-review pilot 3 篇评估
- **result.json 端到端贯通**:researcher 入库 → curator 评分 → 主 agent 引用决策

---

## 派发子 agent 的标准做法

```python
# 阿呆(主 agent)在对话里用 first-class tool
sessions_spawn(
    task="<Mission Briefing 全文>",
    taskName="agent-corpus-curator-t1",
    runtime="subagent",
    cleanup="delete",    # 跑完自动清理
)
```

sessions_spawn tool 的优势:
- 凭证安全--用当前 agent 上下文凭证,scope 小
- 上下文可控--可 `context: "fork"` 选择隔离或继承
- 审计完整--OpenClaw runtime 全结构化日志
- 错误处理链--runtime 完整异常链(非 subprocess 退出码)
- 延迟低--runtime 直接调度,无需进程 fork + CLI 解析

---

## Plugin 接口(corpus-query-tool,2026-08-16 上线)

主 agent / researcher / curator / sci-writing-expert 都可**绕过 sessions_spawn 直接调 5 个 tool**查 corpus。
Plugin 源码:`scripts/corpus/plugin/`(OpenClaw plugin,已在 openclaw.json `plugins.entries.corpus-query-tool` 启用)。

| Tool | 用途 | 输入 | 输出 |
|---|---|---|---|
| `corpus_query` | 查外部文献(混合召回 + 3 维评分 + recommendation) | project, query, top_k?, pmid_filter?, rerank_mode? | {results: [{pmid, recommendation, relevant_chunks[]}]} |
| `corpus_search_self` | 查自写文档(tree rerank) | project, query, top_k?, level?, expand_context? | {results: [{chunk_id, score, path, heading_chain, context?}]} |
| `corpus_get_chunk` | 取 chunk 完整 tree 信息(树位置) | chunk_id, include? (text/snippet/metadata/parent/children/siblings) | {chunk_id, text?, metadata?, parent?, children?, siblings?} |
| `corpus_list_similar_level` | 召回同层级相似 chunk | chunk_id, top_k?, same_doc? | {reference, results: [{chunk_id, score, jaccard}]} |
| `corpus_score` | 重打分文献(写,quality evidence) | project, pmid, criterion_validity, outcome_reliability, conclusion_data_consistency, notes? | {pmid, quality_final} |

### 使用 SOP

| 场景 | Tool 调用 | 何时用 |
|---|---|---|
| "这篇文献之前看过吗" | `corpus_query(project, query=paper_title)` | 主 agent 收到引用请求前先核 |
| "我自己写过类似章节吗" | `corpus_search_self(project, query, level=2)` | 写综述/论文时检查自我重复 |
| "这个 chunk 在文档哪里" | `corpus_get_chunk(chunk_id, include=[parent,children,siblings])` | 召回前先理解 tree 位置 |
| "召回同层级的章节" | `corpus_list_similar_level(chunk_id)` | 综述写"其他论文 Limitations" |
| "重算一篇的评分" | `corpus_score(project, pmid, ...)` | curator 评分 |

### 与 sessions_spawn 的分工

- **读查询**(query / search-self / get-chunk / list-similar-level)→ **直接调 plugin tool**(快,不拉 subagent)
- **写操作**(ingest / register-raw / process-raw / score)→ **走 sessions_spawn → researcher / curator**(保持 SOP 边界 + 审计)
- **深度评分**(evidence features 重算 + key_findings 抽取)→ **走 sessions_spawn → corpus-curator**(LLM 推理,不是简单 tool)

## 文件位置速查

| 文件 | 用途 |
|---|---|
| `scripts/corpus/db.py` | CorpusDB class + 5 表 + V2 tree 字段 + snippet + aggregate + recommendation |
| `scripts/corpus/cli.py` | init / register-raw / process-raw / ingest-doc / query / search-self / answer-self / score / group / list-projects / list-groups |
| `scripts/corpus/openalex.py` | OpenAlex 期刊 IF 查询 |
| `scripts/corpus/chunker_markdown.py` | V2 heading-aware chunker（400 字符阈值 + tree metadata + JSONL 输出） |
| `scripts/corpus/chunker_markdown.v1.py.bak` | V1 chunker 备份 |
| `scripts/corpus/migrate_v2.py` | V2 schema 迁移脚本（幂等） |
| `scripts/corpus/test_chunker_unit.py` | V2 chunker 单测（40 PASS） |
| `/root/.openclaw/workspace-researcher/AGENTS.md` | researcher 现行代理文件（v3，2026-07-22 含 XML > PDF > HTML 优先级 + 落盘位置） |
| `/root/.openclaw/workspace-corpus-curator/AGENTS.md` | corpus-curator 现行代理文件（v3，2026-07-22 明确「不落 raw」+「只读 raw」边界） |
| `agents/master.md` | 主 agent 团队表 + 技能分配 |
| `agents/SCHEMA.md` | JSON schema 协议（researcher v2 + curator v1） |
| `projects/_corpus/corpus.db` | **统一文献库**（所有项目共享 chunks + vec0） |
| `projects/_corpus/raw/<pmid>.md` | 提取后文本（V2 chunker 输入；项目级原始文件不在这里，原始文件走 `projects/<slug>/knowledge/fulltext/<pmid>.{xml,pdf,html}`） |

---

## 关键决策点(不要再重新讨论)

1. **vector DB 选 sqlite-vec**(不是 lancedb / qdrant)-- 单文件，跟 metadata.sqlite 合并部署
2. **chunk 切片统一走 heading-aware**(2026-07-21 V3)-- .md 走 V2 chunker，.txt 走 V3 plain-text IMRAD regex chunker，两者产出同名树结构供下游一致使用
3. **PDF 解析统一走 Docling**(2026-07-21 V3.1 选定)-- `corpus process-pdf` 一行命令 PDF->.md，不装额外的 mineru / marker-pdf；Docling 在 CPU pipeline 上比 MinerU 快 3-10x 且无 IO 死锁
4. **snippet 提取 MVP 用 chunk 第一句**(不用 heading 下第一句)-- chunk_text 通常不含 heading
5. **embedding 用 qwen text-embedding-v4**(dim=1024, 走 qwen-dashscope provider, 标准 dashscope key)-- mock 模式仅作 emergency fallback
6. **rerank 默认走 dashscope qwen3-rerank**(2026-07-21 接入)-- linear (0.6×vec+0.4×kw) 保留作 --rerank-mode linear 回退选项
7. **heading_match 不命中 + text_keyword_hit 命中 = 不降权**-- MVP 加了 text 关键词补偿规则，避免误伤
8. **venue IF 缺失 = 不惩罚**-- unknown IF 不影响准入
9. **不调 OpenAlex 仍可入库**-- venue 字段全 unknown，但 corpus 仍可用
10. **agent 注册 5 件齐**(SOUL + config + master 表 + 技能表 + openclaw.json agents.list)
11. **派子 agent 一律用 sessions_spawn tool**(唯一路径，无外置脚本替代)
12. **process-raw 幂等**(可重复跑) -- 重入库会先 DELETE 现有 chunks/vec 再重 embed
13. **plugin 化开放"主 agent 调 corpus"限制**(2026-08-16 L 拍板) -- 主 agent 可直接调 `corpus-query-tool` 5 个 tool 做查询;写操作(ingest/register-raw)仍走 sessions_spawn → researcher
14. **corpus 不进 memory_search**(2026-08-16 L 拍板) -- corpus 是独立知识库,由 corpus-query-plugin 提供 tool 接口;main agent 的 memory_search 不召回 corpus;`memory/corpus_xxx.md` 已物理移走

---

## 常见任务模板

### 任务 1: 注册新项目
```bash
python3 scripts/corpus/cli.py init --project <slug> --title "项目标题" --status active
```

### 任务 2: researcher 入库 1 篇(一次传完整字段)
```bash
python3 scripts/corpus/cli.py ingest --project <slug> \
  --pmid <PMID> --query "<query>" --source pubmed \
  --title "<真实标题>" \
  --abstract-excerpt "<≤500 字摘要>" \
  --doi "10.xxxx/xxx"
```

### 任务 3: curator 评分(3 个 evidence features)
```bash
python3 scripts/corpus/cli.py score --project <slug> --pmid <PMID> \
  --criterion 0.85 --outcome 0.75 --conclusion 0.70 \
  --notes "clear design, validated assay, conclusion follows data"
```

### 任务 4: 主 agent 拿引用
```bash
python3 scripts/corpus/cli.py query --project <slug> \
  --query "<检索 query>" --top 10 \
  | python3 -c "import json, sys; r = json.load(sys.stdin); print(json.dumps(r['results'], indent=2))"
```

返回每条含 `recommendation` (primary/supporting/background) + `relevant_chunks[].snippet` 主 agent 直接引用

### 任务 5: 管理项目分组
```bash
# 列出所有项目
python3 scripts/corpus/cli.py list-projects

# 列出项目内的文献
python3 scripts/corpus/cli.py list-groups --project <slug>

# 手动把文献加入项目(或改 role)
python3 scripts/corpus/cli.py group --project <slug> --pmid <PMID> --role core
```

### 任务 5: 派子 agent
```python
# 阿呆(主 agent)在对话里
sessions_spawn(
    task=build_mission_briefing(mission, task),  # 完整 Mission Briefing
    taskName=f"agent-{task['assignee']}-{task['id']}",
    runtime="subagent",
    cleanup="delete",
)
```

### 任务 6: V2 入库自写文档(sci-writing-expert 专用)
```bash
python3 scripts/corpus/cli.py --embedding-mode qwen ingest-doc \
  --file <path>/draft_ch5.md \
  --project <slug>
# pmid 自动生成:selfw__<doc_slug>__<basename>
# embedding 走 qwen text-embedding-v4(dim=1024),默认 mode=qwen
# 不查 OpenAlex(source=self_written,venue 为空)
# 不入 document_groups 需手动调 corpus group(脚本已自动以 role=core 插入)
```

### 任务 7: V2 检索 self_written(sci-writing-expert Stage 0 / answer-self)
```bash
# 只检索 tree metadata,不调 LLM
python3 scripts/corpus/cli.py --embedding-mode qwen search-self \
  --project <slug> --query "<natural language>" --top 5 \
  [--level 0|1|2|3] [--expand-context] [--format json|text]

# RAG 检索 + LLM 回答(deepseek-v4-flash 默认)
python3 scripts/corpus/cli.py --embedding-mode qwen answer-self \
  --project <slug> --query "<question>" --top 5 \
  [--llm deepseek] [--llm-model deepseek-v4-flash] \
  [--show-prompt] [--run-llm] [--format json|text]
```

### 任务 8: V2 迁移 (一次性,已跑)
```bash
python3 scripts/corpus/migrate_v2.py
# ALTER TABLE chunks 加 7 列 + 3 索引;幂等
```

---

## 失败模式(实测发现)

| 失败 | 表现 | 修复 |
|---|---|---|
| OpenAlex 慢导致 register-raw 卡 | 串行 3 篇 5 分钟超时 | 加 venue timeout + 并发 register-raw |
| sqlite-vec KNN 语法 | `LIMIT ?` 报错 | 必须用 `AND k=?` 形式 |
| `_first_sentence` 长句 | 130 字不 trim 完整 | 超过 target+15 降级到 fallback |
| heading_match 严格性 | 真相关被 0.5 折扣 | 加 text_keyword_hit 补偿 |
| type_prior=unknown | evidence 评分救场 | max(prior, evidence_mean) 策略 |
| 外键约束失败 | chunks 引用 documents | register-raw 必须先于 process-raw |
| **agent 没注册到 openclaw.json:agents.list** | `openclaw agents list` 看不到 | **必须 5 件齐 + dry-run 验证** |
| **sessions_spawn 失败** | subagent 跑不起来 | 检查 agentId 是否在 openclaw.json agents.list + subagents.allowAgents 里 |

---

## v1 演进方向(不阻塞)

1. cli.py register-raw 加 `--no-venue` / `--venue-timeout` / 并发 register-raw
2. 接 JCR/CAS 表做 venue IF 映射
3. heading-aware 切片(保留 heading 在 chunk_text 开头)
4. curator 自动 key_findings / quote_candidates 抽取(用 LLM)
5. ~~跨项目 corpus 合并查询~~ **已实现**:统一 corpus.db + document_groups 表
6. 跑更多 pilot(nsfc-egfr / lipo-degradation)

---

## 参考

- OpenAlex API: https://api.openalex.org (无 key, 10 req/s polite pool)
- sqlite-vec: https://github.com/asg017/sqlite-vec
- PRISMA 2020: http://prisma-statement.org/
- 已实装 corpus: `projects/ar-review/corpus/` (3 PMIDs, 15 chunks)
- 端到端测试:sessions_spawn → researcher 入库 → curator 评分 → primary / supporting / background
