# PRODUCT.md — Corpus 文献内容库 · 产品设计（阶段 2）

> 状态: **草案 v0.1** | 2026-09-05 | 由 L 产品理念 → 阿呆整理
> 本文档是 5 阶段流程（GIT → **产品设计** → 技术方案 → 代码书写 → 项目验证）的阶段 2 产物。
> 目标读者: L（产品决策人）/ 后续阶段执行 agent。

---

## 1. 产品定位

**一句话**: 一个个人化的**文献内容库** —— 解析文献 → 结构级切片存储 → 位置感知内容召回 → 质量评分,四个环节形成一个完整的文献知识管道。

**与现有产品的区别**:
- 旧 corpus: 只有「知识获取 + 召回」单一功能（`corpus_query` 检索工具）
- 新 corpus: 完整的**文献内容生命周期管理**（读取 → 切片 → 存储 → 召回 → 评估）

**不是**: 不是项目管理工具（那是 fact-infra 的职责,不联动）、不是通用文档库、不是企业级 RAG 平台。

---

## 2. 背景:灵感源是 RAGFlow,但砍掉多余

corpus 最初**基于 RAGFlow 的灵感**构建,目标是个人化的轻量版,因为 RAGFlow 的功能对我们来说有点多余。

### 2.1 参考了 RAGFlow 什么（核心思路,全保留）

| RAGFlow 特性 | corpus 对应物 | 说明 |
|---|---|---|
| 深度文档理解（DeepDoc） | **Docling**（已集成,2.124.0） | heading 层级 / 表格 / 公式 / 图片引用,全离线 CPU |
| 可溯源引用（Grounded citations） | `chunk_id` + `snippet_source` + `heading_path` + `heading_chain` | 召回结果自带位置溯源,降幻觉 |
| 混合召回 + 融合重排 | vector KNN + keyword LIKE + rerank（linear/dashscope） | 多路召回再重排 |
| 模板化分块（论文模板） | tree-aware Markdown chunker（heading 层级驱动,非模板套用） | 文献专用,按结构切片 |
| 多 embedding 可选 | Qwen text-embedding-v4（1024 维,默认） | 可换 |

### 2.2 砍掉了什么（为什么）

| RAGFlow 功能 | 砍掉原因 |
|---|---|
| Web UI / 可视化 chunk 检查器 | 我们是 agent + CLI 工作流,不需要人机界面 |
| 多数据源（Word/Excel/PPT/扫描件 + 外部同步） | 聚焦**文献**（PDF 为主 + 自写 MD） |
| 知识图谱 / 知识编译引擎（Wiki/Graph/MindMap/Timeline/Skills） | 对我们多余,transformer 类语义检索够用 |
| 多租户 / 权限 / 多用户 | 个人库 |
| Elasticsearch / 分布式向量库（Qdrant/Weaviate） | SQLite + sqlite-vec 单机足够 |
| Docker 全栈部署（RAM ≥16GB） | 我们跑在 OpenClaw 沙箱,轻量原生 |

---

## 3. 核心能力支柱（4 个）

### 支柱 A: 文献读取解析（Ingest）
- **目标**: 任意文献格式 → **canonical 结构表示**（结构化中间层），保留原始结构
- **现状**: `process-pdf`（Docling PDF→MD，heading-aware，仅 .pdf）/ `register-raw`（注册 raw+元数据）/ `jats_to_md`（JATS XML→MD）✅ 已实现
- **格式范围**（L 已拍板 2026-09-05）: **PDF + DOCX + HTML** + JATS XML + 自写 MD。不设限「只 PDF」，内容来源多样化（补充材料/网页/稿件都可能）。
- **⚠️ Canonical schema（Codex 审查 P0，阶段 3 前置）**: 多格式不能直接拼 Markdown 喂 chunker（DOCX 样式名≠语义 heading、HTML 有导航/广告/隐藏节点）。需定义 canonical schema（节点类型 / heading level/path / 正文 / table / formula / caption / source locator / parser warnings），chunker 只依赖该 schema。入口建议改名 `process-document --format auto|pdf|docx|html|jats|md`（保留 process-pdf 兼容别名）

### 支柱 B: 结构级切片存储（Chunk + Index）
- **目标**: 切片时**尽可能保留语义完整 + 保留其结构**
- **现状**: tree-aware chunker（heading 层级驱动）+ embedding 入库,`chunks` 表存 cookie 字段:
  - 结构: `level` / `path` / `parent_id` / `child_ids` / `sibling_ids` / `heading_chain` / `heading_path`
  - 语义: `text` / `snippet` / `snippet_source` / `section_importance`
  - 向量: `chunk_vectors`（vec0 虚拟表,1024 维 Qwen）
- **关键约束**（L 提出的设计原则）: 切片粒度必须让**语义单元完整**，不能把一段话/一个论点切碎;结构信息必须完整保留，供召回时定位
- **⚠️ Generation 化（Codex 审查 P0，阶段 3 前置）**: chunk ID 应含「document content hash + 规范化路径 + chunker 版本」；re-chunk 时先建新 generation，完成 embedding/index 后**原子切换 active_generation**，再异步清理旧代。重处理不得静默覆盖旧版本

### 支柱 C: 位置感知内容召回（Retrieve）
- **目标**: 召回时不仅返回内容本身,还能:
  - 知道内容**在文献的什么位置**（section、heading 链、层级）
  - **召回其前后文**（parent / siblings / children）做进一步分析
- **现状**:
  - `query`: 混合检索 + 3 维评分排序 ✅
  - `corpus_get_chunk`: 取 chunk + 完整树上下文（parent/children/siblings）✅
  - `corpus_list_similar_level`: 同层级跨文献平行 section 召回 ✅
  - `search-self` / `answer-self`: 自写文档 RAG 检索 + LLM 生成 ✅
- **缺口**（L 已拍板 2026-09-05）: **召回默认不带前后文**。主 `query` 保持轻量（评分排序结果），位置信息/树上下文（parent/siblings/children）是**按需工具** `corpus_get_chunk` 的职责，不需要默认带上。
- **引用脉络检索**（researcher 工作流,非 corpus 新功能）: 从核心文献出发,用 PubMed `elink`（`pubmed_pubmed_refs` / `pubmed_pubmed_citedin` / `-related`）追踪引用上游（证据源头）/ 下游（后续发展）/ 旁路（相似文献） → 筛选 → `register-raw` 入库。**现在就可做,不依赖产品开发**

### 支柱 D: 质量评分（Quality Scoring）
- **目标**: 对文献质量做评估,**增加召回内容的可信度** —— 召回结果按质量排序,让 agent 优先采信高质量文献
- **现状**: curator 对 3 个特征打分（0-1）:
  - `criterion_validity`（标准有效性）
  - `outcome_reliability`（结局可靠性）
  - `conclusion_data_consistency`（结论-数据一致性）
  - 公式: `evidence_mean = mean(3 特征)`;`quality_final = max(quality_type_prior_score, evidence_mean)`
  - type_prior 来自 article_type（meta_analysis→high / rct→high / cohort→medium...）
- **⚠️ 公式版本化（Codex 审查 P0，阶段 3 前置）**: 权重不得硬编码——保存 `formula_version` / `prior_weight` / `evidence_weight` / `rubric_version` / `scorer_id` / `scored_at`，新公式上线可重算与审计。评审后代码实现与文档拍板不一致 = P0 风险，阶段 4 落地时一并迁移。
- **⚠️ 冷启动可见性（Codex 审查 P0）**: 无打分 fallback 时返回 `quality_status=prior_only`，不得伪装成已审核；`evidence_n` < 3 时降低置信度，默认不允许进 primary（除非显式人工 override）

---

## 4. RAG 管线映射

```
┌────────────┐   ┌────────────┐   ┌───────────┐   ┌────────────┐   ┌───────────┐   ┌──────────┐
│  Ingest    │ → │   Chunk    │ → │  Embed    │ → │  Index     │ → │ Retrieve  │ → │ Rerank   │
│ 解析文献   │   │ 结构切片    │   │ 向量化    │   │ 存储       │   │ 召回      │   │ 重排     │
└─────┬──────┘   └─────┬──────┘   └─────┬─────┘   └─────┬──────┘   └─────┬──────┘   └────┬─────┘
      │ process-pdf    │                │                │                │              │
      │ register-raw   │ process-raw    │ qwen v4 API    │ SQLite files   │ query        │ linear /
      │ jats_to_md     │ ingest-doc     │ (urllib)       │ (db.py 5 表)   │ search-self  │ dashscope
      └────────────────┘                └────────────────┘ get/similar   └──────────────┘
```

现有 CLI 13 个子命令按管线分组:

| 管线阶段 | 命令 |
|---|---|
| Ingest | `init` / `register-raw` / `process-pdf`(Docling)/ `process-raw` / `ingest-doc` |
| Chunk+Embed+Index | `process-raw` 内部(chunker + embedding + 入库) |
| Retrieve | `query` / `search-self` / `get`(chunk_query.py)/ `similar`(chunk_query.py) |
| Rerank+Generate | `query --rerank-mode` / `answer-self`(LLM 生成) |
| Score | `score` |
| 项目管理(旧,属 fact 域) | `group` / `list-projects` / `list-groups` |

---

## 5. 现状 vs 目标（Gap 分析）

| 能力 | 现状 | 缺口 | 优先级 |
|---|---|---|---|
| A 解析 | Docling PDF / JATS / MD | **多格式 canonical schema 未定义**（PDF/DOCX/HTML 需统一中间表示） | P0 |
| B 切片 | tree-aware,结构字段全 | **Generation/re-chunk 原子切换未设计**;chunk ID 需含 content hash + chunker 版本 | P0 |
| C 召回 | 5 个工具,位置 + 树上下文可用 | 主 query 默认不带位置/前后文（L 已拍板保持轻量，按需 get_chunk）✅ | P1 |
| D 评分 | 3 特征 + max 公式（旧） | **公式已拍板 0.7/0.3 但代码未改**;需 formula_version + prior_only 冷启动标记 | P0 |
| E 可观测性（新增） | 无 | **ingestion_jobs 状态机缺失**（discovered→parsed→chunked→embedded→indexed→failed）;错误/重试/版本不可见 | P0 |
| F 一致性（新增） | 无 | **fsck 缺失**（chunks↔vec0 orphan 校验）;备份/恢复演练未定义 | P1 |
| G 纠错闭环（新增） | 无 | 无 UI 但有 CLI 即可: 标记坏 chunk / 重跑单文档 / 导出抽样报告 | P1 |

---

## 5.1 横切能力（Codex 审查 2026-09-05 采纳）

1. **可观测性**: `ingestion_jobs` manifest（状态 / 错误 / 重试次数 / 输入 hash / parser/chunker/embedder 版本）——出错时 agent 能看到半成品而不是瞎猜
2. **一致性**: 开启 foreign_keys；`fsck` 检测向量孤儿/重复/维度错误；embedding 失败只能留下可重试 job，不得留下「已索引」状态
3. **纠错闭环**: CLI 提供查看/标记坏 chunk、重跑单文档、导出抽样报告
4. **引用工作流边界**: researcher 通过 PubMed elink 得到的引用关系存为外部工作流产物（含抓取时间/来源），**不得混入** corpus 的相关度或 quality，避免排序语义漂移
5. **数据一致性**: documents.article_type/prior 修改 → 标记 quality_evidence stale 按 formula_version 重算；relevance 随 chunk generation 标 stale 不得复用
6. **备份/恢复**: 在线一致性快照、schema 版本、vec0 重建步骤、恢复演练（P0）

---

## 6. Non-Goals（明确不做）

1. **fact-infra 联动**: corpus 只做文献库,fact 只做项目管理,两边不互相依赖
2. **文献相关格式专用**: 不追求通用文档库（扫描 OCR / PPT / Excel 不属于文献内容），边界 = 文献相关格式（PDF/DOCX/HTML/JATS/MD，已拍板）
3. **知识图谱 / 实体关系抽取**: 不做 RAGFlow 的 Graph/Wiki/Timeline 类知识编译
4. **Web UI / 可视化**: agent + CLI 是唯一界面
5. **多租户 / 权限 / 多用户**: 个人单用户库
6. **分布式向量库 / Elasticsearch**: SQLite + sqlite-vec 单机足够

---

## 6.1 Codex 审查结论（2026-09-05，全文见 docs/review/codex-review-2026-09-05.md）

**判定: 条件通过（可进阶段 3）**，前置条件（已采纳进本文档）：
1. 评分公式设计↔实现一致性 → formula_version + 迁移/回算（§3 支柱 D）
2. 多格式 canonical schema + 输入 hash + generation/re-chunk 原子切换（§3 支柱 A/B）
3. Python worker/IPC 契约 + spawn 降级路径 + 并发/超时语义（阶段 3 ARCHITECTURE.md 定）
4. 量化验收门槛（阶段 5）：Recall@10≥0.85 / MRR@10≥0.70 / 标题树准确率≥95% / fsck 0 orphan / 评分 Spearman ρ≥0.70，详见审查报告验收矩阵

---

## 7. 开放问题（需 L 拍板）

- **Q1 — 评分公式** ✅ L 已拍板 2026-09-05: 有 curator 打分时 `quality = 0.7×evidence_mean + 0.3×prior`，无打分 fallback 到 prior（0.7/0.3 为初始值，阶段 5 回测可调）。原 `max(prior, evidence)` 记为废弃（type 标签会架空 curator 打分，失去区分度）。
- **Q2 — 召回默认带位置+前后文?** ✅ L 已拍板 2026-09-05: **不默认带**。主 `query` 保持轻量，前后文扩展走按需工具 `corpus_get_chunk`。
- **Q3 — 格式边界** ✅ L 已拍板 2026-09-05: 支持 **PDF + DOCX + HTML** + JATS + MD，内容来源不限于 PDF。
- **Q0 — 引用层要不要结构化?**（2026-09-05 已确认方向:引用追踪走 researcher 工作流,暂不做系统级）。触发再做信号: 文献库数百篇 / 需要被引次数做排序特征 / 需要一键跳引用网络。
- **Q4 — 切片语义完整性验证方法** ✅ L 已拍板 2026-09-05: 「自动单测守底线 + 按格式类型低频人工抽查」。chunker 是确定性规则算法（无 LLM 调用），一次验收某格式 = 该类所有文献复用；日常入库不抽查、不阻塞。详见 §5 Gap 分析 B 行。

---

**判定: 条件通过（可进阶段 3）**，前置条件（已采纳进本文档）：
1. 评分公式设计↔实现一致性 → formula_version + 迁移/回算（§3 支柱 D）
2. 多格式 canonical schema + 输入 hash + generation/re-chunk 原子切换（§3 支柱 A/B）
3. Python worker/IPC 契约 + spawn 降级路径 + 并发/超时语义（阶段 3 ARCHITECTURE.md 定）
4. 量化验收门槛（阶段 5）：Recall@10≥0.85 / MRR@10≥0.70 / 标题树准确率≥95% / fsck 0 orphan / 评分 Spearman ρ≥0.70，详见审查报告验收矩阵

---

## 8. 名词表

| 术语 | 含义 |
|---|---|
| chunk | 文献按结构切出的最小检索单元（带 heading 上下文） |
| heading tree | 文献标题层级树（H1→H2→H3）,决定 chunk 的 parent/child/sibling |
| vec0 | sqlite-vec 的虚拟表（向量最近邻检索） |
| type_prior | 由文献类型（meta_analysis/rct/cohort...）推导的质量先验 |
| evidence_mean | curator 打分 3 特征的均值 |
| quality_final | 最终质量分（旧公式 = max(prior, evidence)） |
| RAG | Retrieval-Augmented Generation,检索增强生成 |

---

*下一步: L 确认/修改本草案 → 关闭或重排开放问题 → 进入阶段 3（技术方案 ARCHITECTURE.md）*