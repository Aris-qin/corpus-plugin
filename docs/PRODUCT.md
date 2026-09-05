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
- **目标**: 任意文献格式 → 结构化 Markdown,保留原始结构
- **现状**: `process-pdf`（Docling PDF→MD，heading-aware）/ `register-raw`（注册 raw+元数据）/ `jats_to_md`（JATS XML→MD）✅ 已实现
- **格式范围**（L 已拍板 2026-09-05）: **PDF + DOCX + HTML** + JATS XML + 自写 MD。不设限「只 PDF」，内容来源多样化（补充材料/网页/稿件都可能）。corpus-query 现有 `process-pdf` 命令只处理 .pdf，**需扩展为多格式入口**（Docling 本身支持，属阶段 4 改造项）

### 支柱 B: 结构级切片存储（Chunk + Index）
- **目标**: 切片时**尽可能保留语义完整 + 保留其结构**
- **现状**: tree-aware chunker（heading 层级驱动）+ embedding 入库,`chunks` 表存 cookie 字段:
  - 结构: `level` / `path` / `parent_id` / `child_ids` / `sibling_ids` / `heading_chain` / `heading_path`
  - 语义: `text` / `snippet` / `snippet_source` / `section_importance`
  - 向量: `chunk_vectors`（vec0 虚拟表,1024 维 Qwen）
- **关键约束**（L 提出的设计原则）: 切片粒度必须让**语义单元完整**,不能把一段话/一个论点切碎;结构信息必须完整保留,供召回时定位

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
- **⚠️ 旧评分是否适合:未知**（L 提出,开放问题 Q1）:
  - 现在的 `max(prior, evidence)` 让「类型先验」可能盖过 curator 实际判断（一篇烂 meta-analysis 仍会被 prior 抬到 high）
  - 需要验证: 三个特征维度是否真的代表「文献可信度」?公式是否要重设计?

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
| A 解析 | Docling PDF / JATS / MD | 格式边界待定（是否要 DOCX/HTML） | P2 |
| B 切片 | tree-aware,结构字段全 | **语义完整性无验证**（没有 chunk 质量评估机制） | P1 |
| C 召回 | 5 个工具,位置 + 树上下文可用 | 主 query 召回默认不带位置/前后文;前后文「进一步分析」没自动化（要 agent 自己再调 get_chunk） | P1 |
| D 评分 | 3 特征 + max 公式 | **公式合理性未验证**;评分可信度如何注入召回排序 | P1 |

---

## 6. Non-Goals（明确不做）

1. **fact-infra 联动**: corpus 只做文献库,fact 只做项目管理,两边不互相依赖
2. **通用文档库**: 不追求 Word/Excel/PPT/扫描件全格式,聚焦文献（PDF 为主）
3. **知识图谱 / 实体关系抽取**: 不做 RAGFlow 的 Graph/Wiki/Timeline 类知识编译
4. **Web UI / 可视化**: agent + CLI 是唯一界面
5. **多租户 / 权限 / 多用户**: 个人单用户库
6. **分布式向量库 / Elasticsearch**: SQLite + sqlite-vec 单机足够

---

## 7. 开放问题（需 L 拍板）

- **Q1 — 评分公式** ✅ L 已拍板 2026-09-05: 有 curator 打分时 `quality = 0.7×evidence_mean + 0.3×prior`，无打分 fallback 到 prior（0.7/0.3 为初始值，阶段 5 回测可调）。原 `max(prior, evidence)` 记为废弃（type 标签会架空 curator 打分，失去区分度）。
- **Q2 — 召回默认带位置+前后文?** ✅ L 已拍板 2026-09-05: **不默认带**。主 `query` 保持轻量，前后文扩展走按需工具 `corpus_get_chunk`。
- **Q3 — 格式边界** ✅ L 已拍板 2026-09-05: 支持 **PDF + DOCX + HTML** + JATS + MD，内容来源不限于 PDF。
- **Q0 — 引用层要不要结构化?**（2026-09-05 已确认方向:引用追踪走 researcher 工作流,暂不做系统级）。触发再做信号: 文献库数百篇 / 需要被引次数做排序特征 / 需要一键跳引用网络。
- **Q4 — 切片语义完整性验证方法**: 怎么判断「切片保留了语义完整」?（人工抽查 / 召回质量回测 / 单元测试）——**待定**

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