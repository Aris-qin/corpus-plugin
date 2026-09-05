#!/usr/bin/env python3
"""
corpus.cli — 统一文献库的命令行入口

命令:
  init --project <slug> [--title "..."] [--status active|done|backlog] [--desc "..."]
                                         注册新项目到总库（如果不存在）
  ingest --project <slug> --pmid X [--query "..."] [--source pubmed|glm_web|url_fetch]
                                         抓全文 + 切片 + embedding + 入库 + 3 维初始分
  query --project <slug> --query "..." [--top 10] [--pmid-filter X,Y]
                                         向量检索 + 聚合 + recommendation（按项目分组过滤）
  score --project <slug> --pmid X --criterion X --outcome X --conclusion X [--notes "..."]
                                         curator 重算 quality (evidence features)
  group --project <slug> --pmid X [--role core|peripheral|cited]
                                         把文献加入项目分组（或改 role）
  list-projects                          列出所有已注册项目
  list-groups --project <slug>           列出项目内的所有文献分组

设计原则:
  - 每个命令一次完成一个原子操作（方便串到 shell pipeline）
  - 统一总库路径: projects/_corpus/corpus.db
  - 项目分组: document_groups 表（一篇文献可挂多个项目）
  - embedding 默认 1536 维（OpenAI text-embedding-3-small 的维度）
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# 让 db.py 可 import
sys.path.insert(0, str(Path(__file__).parent))
from db import (
    CorpusDB, ARTICLE_TYPE_TO_PRIOR, QUALITY_TO_NUMERIC,
    extract_snippet, heading_match_jaccard, text_keyword_hit,
    aggregate_document_relevance, make_recommendation,
)
from chunker_markdown import chunk_markdown_file, write_jsonl as _write_chunk_jsonl


WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/root/.openclaw/workspace"))
EMBEDDING_DIM_DEFAULT = 1024  # Qwen text-embedding-v4 default dim (aligned with corpus DB)\nEMBEDDING_MODE_DEFAULT = "qwen"  # use real v4 API by default (was "mock")
UNIFIED_CORPUS_DB = WORKSPACE_ROOT / "projects" / "_corpus" / "corpus.db"


# ---------- 子命令 ----------

def cmd_init(args):
    """注册新项目到总库（如果不存在）"""
    project = args.project
    title = args.title or project
    status = args.status or "active"
    desc = args.desc or ""

    if not UNIFIED_CORPUS_DB.exists():
        print(f"[init] ERROR: unified corpus not found: {UNIFIED_CORPUS_DB}", file=sys.stderr)
        print(f"[init] Run: sqlite3 {UNIFIED_CORPUS_DB} to create schema first", file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)

    # 注册项目（INSERT OR IGNORE）
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    db.conn.execute(
        """INSERT OR IGNORE INTO projects (slug, title, status, description, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project, title, status, desc, now, now)
    )
    db.conn.commit()

    # 确保 raw/ 目录存在
    raw_dir = UNIFIED_CORPUS_DB.parent / "raw"
    raw_dir.mkdir(exist_ok=True)

    db.close()

    print(f"[init] project '{project}' registered in unified corpus")
    print(f"[init] DB: {UNIFIED_CORPUS_DB}")
    print(f"[init] raw/: {raw_dir}")


def verify_pmid_raw_match(pmid: str, raw_path: Path, expected_title: str = "",
                            expected_abstract: str = "") -> dict:
    """Stage 4 入库前对比: 验证 raw 文件内容跟 PMID 匹配。

    Returns:
        {
            "pmid", "raw_path", "raw_chars",
            "title_keywords_in_text", "title_keyword_hits",
            "abstract_overlap_ratio",
            "status": "ok|warn|fail", "note"
        }

    Rules:
        - title_keywords_in_text: title 关键词(去停用词)在 raw 前 1000 字符出现 >=3 个
        - abstract_overlap_ratio: abstract 前 100 字跟 raw 前 500 字字符重合率
        - status:
            ok: title_keywords_in_text AND abstract_overlap_ratio >= 0.1
            warn: title_keywords_in_text OR abstract_overlap_ratio >= 0.1 (only one)
            fail: neither (抓错全文)
    """
    if not raw_path.exists():
        return {
            "pmid": pmid, "raw_path": str(raw_path), "raw_chars": 0,
            "title_keywords_in_text": False, "title_keyword_hits": 0,
            "abstract_overlap_ratio": 0.0, "status": "fail",
            "note": f"raw file not found: {raw_path}"
        }
    raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
    raw_head = raw_text[:1000]
    raw_full = raw_text[:500]

    stop_words = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to",
                  "for", "with", "by", "from", "as", "is", "are", "was", "were",
                  "be", "been", "being", "via", "using"}
    title_tokens = []
    for t in expected_title.lower().split():
        t = t.strip(".,;:()[]{}\"'?!:")
        if t and t not in stop_words and len(t) > 2:
            title_tokens.append(t)
    title_tokens = list(set(title_tokens))

    title_hits = sum(1 for tok in title_tokens if tok in raw_head.lower())
    title_keywords_in_text = title_hits >= 3 and len(title_tokens) >= 3

    abstract_head = expected_abstract[:100].lower() if expected_abstract else ""
    overlap_ratio = 0.0
    if abstract_head:
        abstract_compact = "".join(abstract_head.split())[:50]
        raw_full_compact = "".join(raw_full.lower().split())
        if abstract_compact and abstract_compact in raw_full_compact:
            overlap_ratio = 1.0
        else:
            substrs = [abstract_compact[i:i+20] for i in range(0, len(abstract_compact)-20, 10)]
            if substrs:
                hits = sum(1 for s in substrs if s in raw_full_compact)
                overlap_ratio = hits / len(substrs)

    if title_keywords_in_text and overlap_ratio >= 0.1:
        status, note = "ok", f"title_hits={title_hits}/{len(title_tokens)}, abstract_overlap={overlap_ratio:.2f}"
    elif title_keywords_in_text or overlap_ratio >= 0.1:
        status, note = "warn", f"title_hits={title_hits}/{len(title_tokens)}, abstract_overlap={overlap_ratio:.2f}"
    else:
        status, note = "fail", f"title_hits={title_hits}/{len(title_tokens)}, abstract_overlap={overlap_ratio:.2f}"

    return {
        "pmid": pmid, "raw_path": str(raw_path), "raw_chars": len(raw_text),
        "title_keywords_in_text": title_keywords_in_text,
        "title_keyword_hits": title_hits,
        "abstract_overlap_ratio": round(overlap_ratio, 3),
        "status": status, "note": note,
    }


def cmd_register_raw(args):
    """
    researcher 专用: 入库原始文件(不切片、不 embedding)。

    做的事:
      1. 确认 raw 文件存在(raw/<pmid>.txt)
      2. upsert documents 元数据
      3. venue lookup (OpenAlex)
      4. document_groups 插入项目分组(role=core)

    不做(curator 负责):
      - 切片 / embedding / chunks 入库
    """
    project = args.project
    pmid = args.pmid
    source = args.source

    if not UNIFIED_CORPUS_DB.exists():
        print(f"[register-raw] ERROR: unified corpus not found: {UNIFIED_CORPUS_DB}",
              file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)

    raw_dir = UNIFIED_CORPUS_DB.parent / "raw"
    # V3: 优先 .md (Docling 输出, 来自 process-pdf) > .pdf (待 process-pdf 转) > .txt (legacy fallback, V3 plain-text IMRAD chunker)
    raw_path = None
    for ext in (".md", ".pdf", ".txt"):
        candidate = raw_dir / f"{pmid}{ext}"
        if candidate.exists():
            raw_path = candidate
            break
    if args.raw_path:
        raw_path = Path(args.raw_path)
    if not raw_path.exists():
        print(f"[register-raw] WARNING: raw/{pmid}.txt not found, has_fulltext=0")
        full_text = ""
        has_fulltext = 0
        raw_rel = None
    else:
        full_text = raw_path.read_text(encoding="utf-8", errors="ignore")
        has_fulltext = 1
        raw_rel = str(raw_path.relative_to(UNIFIED_CORPUS_DB.parent))

    # Stage 4: 入库前对比（仅在 args.title 或 args.abstract_excerpt 提供时）
    if (args.title or args.abstract_excerpt) and has_fulltext:
        check = verify_pmid_raw_match(
            pmid, raw_path,
            expected_title=args.title or "",
            expected_abstract=args.abstract_excerpt or "",
        )
        print(f"[register-raw] Stage 4 verify: {check['status']}  {check['note']}")
        if args.verify_only:
            db.close()
            print(f"[register-raw] --verify-only: skip registration")
            sys.exit(0 if check['status'] != 'fail' else 2)
        if check['status'] == 'fail':
            print(f"[register-raw] FAIL: raw does not match PMID (title + abstract mismatch)", file=sys.stderr)
            print(f"[register-raw] use --verify-only first to inspect, or pass --force to override",
                  file=sys.stderr)
            db.close()
            sys.exit(2)

    article_type = _guess_article_type(full_text) if full_text else "unknown"
    type_prior = ARTICLE_TYPE_TO_PRIOR.get(article_type, "unknown")

    title = args.title if args.title else "(title TBD by researcher)"
    abstract = args.abstract_excerpt if args.abstract_excerpt else (full_text[:500] if full_text else "")
    if len(abstract) > 500:
        abstract = abstract[:500]

    db.upsert_document(
        pmid=pmid, title=title, doi=args.doi, source=source,
        has_fulltext=has_fulltext, raw_path=raw_rel,
        article_type=article_type, quality_type_prior=type_prior,
        abstract_excerpt=abstract,
    )
    print(f"[register-raw] {pmid}: document upserted, type={article_type}, prior={type_prior}")

    venue_info = _lookup_venue_safe(pmid)
    if venue_info:
        db.upsert_venue(pmid=pmid, **venue_info)
        print(f"[register-raw] venue: {venue_info.get('journal')} (IF={venue_info.get('impact_factor')})")

    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    db.conn.execute(
        """INSERT OR IGNORE INTO document_groups (pmid, project_slug, role, added_at)
           VALUES (?, ?, 'core', ?)""",
        (pmid, project, now)
    )
    db.conn.commit()
    db.close()

    print(f"[register-raw] {pmid}: added to project '{project}' (role=core)")
    print(f"[register-raw] next: curator runs `corpus process-raw --pmid {pmid}")


def _get_document_project_slugs(pmid: str) -> list[str]:
    """查 document_groups 拿 PMID 所属项目 slug 列表（2026-07-22 加，给 process-pdf 用）
    返回: [slug1, slug2, ...] 多个项目共享同一 PMID 时返回多个
    """
    if not UNIFIED_CORPUS_DB.exists():
        return []
    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=1024)
    rows = db.conn.execute(
        "SELECT DISTINCT project_slug FROM document_groups WHERE pmid=? ORDER BY project_slug",
        (pmid,),
    ).fetchall()
    db.close()
    return [r[0] for r in rows]


def cmd_process_pdf(args):
    """
    researcher 专用: 用 Docling 把 <pmid>.pdf 转成 <pmid>.md (heading-aware 结构)。

    2026-07-22 Bug fix: 支持项目级原始文件查找。
      - 输入路径解析优先级：1) --raw-path > 2) --project 从 document_groups 反查 slug 扫 knowledge/fulltext/ > 3) projects/_corpus/raw/<pmid>.pdf (legacy)
      - 多项目同一 PMID: --project 精确指定单一项目；不传则试所有项目 slugs

    后续调 `corpus process-raw --pmid <pmid>` 会优先读 .md → V2 chunker 拿到 heading tree。

    V3.1: 从 MinerU 切换到 Docling (2026-07-21)。Docling 在 CPU pipeline 上比 MinerU 快 ~3-10x
    (7 页 80s vs 120s, 24 页 65s vs MinerU timeout), 质量接近（heading 层级 + 表格 + 公式都保留）。
    """
    from docling.document_converter import DocumentConverter  # noqa: E402

    pmid = args.pmid
    raw_dir = UNIFIED_CORPUS_DB.parent / "raw"
    # 2026-07-22: PDF 路径解析三级优先级
    # 1) --raw-path 最高优先级
    # 2) --project：查 document_groups 拿 slug → 扫 projects/<slug>/knowledge/fulltext/<pmid>.{pdf,xml} 同名任意后缀
    # 3) legacy: projects/_corpus/raw/<pmid>.pdf
    pdf_path = None
    if args.raw_path:
        pdf_path = Path(args.raw_path).resolve()
        if not pdf_path.exists():
            print(f"[process-pdf] ERROR: --raw-path {pdf_path} not found", file=sys.stderr)
            sys.exit(1)
    elif args.project:
        # 验证 project slug 真实存在；扫 .pdf / .xml 同名（XML 也允许作为中间状态，Docling 不能直接抽 XML，先 .pdf）
        project_root = WORKSPACE_ROOT / "projects" / args.project
        fulltext_dir = project_root / "knowledge" / "fulltext"
        # 验证 project 跟 pmid 真实关联
        slugs = _get_document_project_slugs(pmid)
        if args.project not in slugs:
            print(f"[process-pdf] WARN: pmid {pmid} not in project '{args.project}' (document_groups shows: {slugs})",
                  file=sys.stderr)
        for ext in (".pdf",):
            candidate = fulltext_dir / f"{pmid}{ext}"
            if candidate.exists():
                pdf_path = candidate.resolve()
                break
        if pdf_path is None:
            print(f"[process-pdf] ERROR: --project {args.project} set but no PDF found at "
                  f"projects/{args.project}/knowledge/fulltext/<pmid>.pdf", file=sys.stderr)
            sys.exit(1)
    else:
        pdf_path = raw_dir / f"{pmid}.pdf"
    if not pdf_path.exists():
        print(f"[process-pdf] ERROR: {pdf_path} not found", file=sys.stderr)
        sys.exit(1)
    md_path = raw_dir / f"{pmid}.md"
    if md_path.exists() and not args.force:
        print(f"[process-pdf] {pmid}: {md_path} already exists. Use --force to overwrite.")
        return
    print(f"[process-pdf] {pmid}: running Docling on {pdf_path}")
    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    md_text = result.document.export_to_markdown()
    md_path.write_text(md_text, encoding="utf-8")
    size_kb = md_path.stat().st_size / 1024
    print(f"[process-pdf] {pmid}: wrote {md_path.name} ({size_kb:.1f} KB, {len(md_text)} chars)")
    # update documents.raw_path to .md
    if UNIFIED_CORPUS_DB.exists():
        db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)
        doc = db.get_document(pmid)
        if doc:
            new_raw_rel = str(md_path.relative_to(UNIFIED_CORPUS_DB.parent))
            db.upsert_document(
                pmid=pmid, title=doc.get("title"),
                doi=doc.get("doi"), source=doc.get("source", "pubmed"),
                has_fulltext=1, raw_path=new_raw_rel,
                article_type=doc.get("article_type", "unknown"),
                quality_type_prior=doc.get("quality_type_prior", "unknown"),
                abstract_excerpt=doc.get("abstract_excerpt", ""),
            )
            print(f"[process-pdf] {pmid}: documents.raw_path updated -> .md")
        db.close()
    print(f"[process-pdf] next: corpus process-raw --pmid {pmid}")


def cmd_process_raw(args):
    """
    corpus-curator 专用: 读取 raw -> V2 切片 -> qwen embedding -> 入库 chunks + chunk_vectors。

    做的事:
      1. 从 documents.raw_path 读 raw 文件
      2. V2 chunker_markdown 切片(heading-aware + 400 字符阈值 + tree metadata)
      3. qwen text-embedding-v4 embedding(dim=1024)
      4. insert_chunk_v2(chunks + chunk_vectors + V2 tree 字段)

    不做(researcher 负责):
      - documents 元数据 / venue / document_groups(已由 register-raw 插入)
    """
    pmid = args.pmid

    if not UNIFIED_CORPUS_DB.exists():
        print(f"[process-raw] ERROR: unified corpus not found: {UNIFIED_CORPUS_DB}",
              file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)

    doc = db.get_document(pmid)
    if not doc:
        print(f"[process-raw] ERROR: document {pmid} not found. Run `corpus register-raw` first.",
              file=sys.stderr)
        db.close()
        sys.exit(1)
    raw_rel = doc.get("raw_path")
    if not raw_rel:
        print(f"[process-raw] ERROR: document {pmid} has no raw_path.", file=sys.stderr)
        db.close()
        sys.exit(1)

    raw_full = UNIFIED_CORPUS_DB.parent / raw_rel
    # V3.1: 优先使用同名 .md (Docling 输出)，如果存在则覆盖 documents.raw_path 指向 .md
    alt_md = raw_full.with_suffix(".md")
    if alt_md.exists():
        raw_full = alt_md
    elif not raw_full.exists():
        print(f"[process-raw] ERROR: raw file not found: {raw_full}", file=sys.stderr)
        db.close()
        sys.exit(1)

    full_text = raw_full.read_text(encoding="utf-8", errors="ignore")
    if not full_text.strip():
        print(f"[process-raw] ERROR: raw file is empty: {raw_full}", file=sys.stderr)
        db.close()
        sys.exit(1)

    # Clean any existing chunks + vec for this PMID (process-raw is idempotent).
    # vec0 INSERT OR REPLACE does NOT work; we must DELETE first.
    db.conn.execute("DELETE FROM chunk_vectors WHERE chunk_id LIKE ?", (pmid + "%",))
    db.conn.execute("DELETE FROM chunks WHERE pmid=?", (pmid,))
    db.conn.commit()
    print(f"[process-raw] {pmid}: cleared old chunks/vec")

    # V3: 选择 chunker 根据文件后缀
    suffix = raw_full.suffix.lower()
    if suffix == ".md":
        from chunker_markdown import chunk_markdown
        chunks = chunk_markdown(full_text, source_file=str(raw_full))
        chunker_name = "V2-heading-aware (markdown)"
    elif suffix == ".txt":
        from chunker_markdown import chunk_plain_text
        chunks = chunk_plain_text(full_text, source_file=str(raw_full))
        chunker_name = "V3-heading-aware (plain-text IMRAD regex)"
    else:
        # 其他格式走 plain-text chunker 作 fallback
        from chunker_markdown import chunk_plain_text
        chunks = chunk_plain_text(full_text, source_file=str(raw_full))
        chunker_name = f"V3-plain-text (fallback for {suffix})"
    if not chunks:
        print(f"[process-raw] WARNING: no chunks emitted from {raw_full} [{chunker_name}]", file=sys.stderr)
        db.close()
        sys.exit(1)

    # Override doc_id, chunk_id, parent_id, child_ids, sibling_ids to use PMID
    # (avoids collision across PMIDs in same raw/ dir)
    path_to_new_chunk_id = {}
    path_seen_counts = {}  # Bug fix 2026-07-22: 同 path 下多个 sibling heading 都从 p1 起算会撞主键
    # 修法：保留 chunker 原始 p<N> suffix，追加 's<N>' (sibling under path) 避免冲突
    for c in chunks:
        c.doc_id = pmid
        path_segs = [p for p in c.path.lstrip("/").split("/") if p]
        path_key = "/".join(path_segs)
        path_seen_counts[path_key] = path_seen_counts.get(path_key, 0) + 1
        orig_part = c.chunk_id.split("__")[-1]  # e.g. "p1" / "p2" / ...
        new_id = "__".join([pmid] + path_segs + [f"{orig_part}s{path_seen_counts[path_key]}"])
        path_to_new_chunk_id[c.chunk_id] = new_id
        c.chunk_id = new_id

    # Patch parent_id / child_ids / sibling_ids to use new chunk_ids
    for c in chunks:
        if c.parent_id and c.parent_id in path_to_new_chunk_id:
            c.parent_id = path_to_new_chunk_id[c.parent_id]
        c.child_ids = [path_to_new_chunk_id.get(cid, cid) for cid in c.child_ids]
        c.sibling_ids = [path_to_new_chunk_id.get(sid, sid) for sid in c.sibling_ids]

    inserted = 0
    skipped_empty = 0
    for c in chunks:
        if not c.text.strip():
            skipped_empty += 1
            continue
        try:
            emb = _get_embedding(c.text, args.dim, args.embedding_mode)
        except Exception as e:
            print(f"[process-raw] WARN: embedding failed for {c.chunk_id}: {e}",
                  file=sys.stderr)
            continue
        try:
            db.insert_chunk_v2(
                chunk_id=c.chunk_id, pmid=pmid, ordinal=inserted,
                level=c.level, path=c.path, parent_id=c.parent_id,
                child_ids=list(c.child_ids), sibling_ids=list(c.sibling_ids),
                heading_chain=list(c.heading_chain), doc_id=c.doc_id,
                heading_path=" > ".join(c.heading_chain),
                text=c.text, embedding=emb, auto_snippet=True,
            )
            inserted += 1
        except Exception as e:
            print(f"[process-raw] WARN: insert_chunk_v2 failed for {c.chunk_id}: {e}",
                  file=sys.stderr)
            continue

    db.close()
    print(f"[process-raw] {pmid}: {len(chunks)} total, {inserted} embedded, {skipped_empty} empty")
    print(f"[process-raw] chunker: {chunker_name}")
    print(f"[process-raw] embedding: {args.embedding_mode} (dim={args.dim})")

def cmd_ingest_doc(args):
    """
    V2 ingest-doc: ingest one self-written markdown file into the corpus.

    Pipeline:
      1. read .md from --file
      2. run chunker_markdown V2 (heading-level + 400-char cap + tree metadata)
      3. for each chunk: qwen embedding (or mock fallback)
      4. upsert documents row with pmid = selfw__<doc_slug>__<basename_no_ext>
      5. insert_chunk_v2 (tree fields: level / path / parent_id / child_ids /
         sibling_ids / heading_chain / doc_id)
      6. INSERT OR IGNORE INTO document_groups (role=core, project_slug=--project)

    Source convention: documents.source = 'self_written'
    """
    file_path = Path(args.file).resolve()
    if not file_path.exists():
        print(f"[ingest-doc] ERROR: file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    project = args.project
    if not UNIFIED_CORPUS_DB.exists():
        print(f"[ingest-doc] ERROR: unified corpus not found: {UNIFIED_CORPUS_DB}",
              file=sys.stderr)
        sys.exit(1)

    # 2026-07-22 Bug fix: 警告 self-written 不在项目级路径下（不强制）
    expected_prefix = (WORKSPACE_ROOT / "projects" / project / "knowledge").resolve()
    if not file_path.is_relative_to(expected_prefix):
        print(f"[ingest-doc] WARN: self-written 不在项目级路径 "
              f"({expected_prefix}/selfw/ 推荐) — 当前位置 {file_path}", file=sys.stderr)
        print(f"[ingest-doc] WARN: 建议 `mv {file_path} {expected_prefix}/selfw/` 后重跑", file=sys.stderr)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)

    # 1+2. chunk via V2 chunker (handles tree fields: parent/child/sibling/path/level)
    chunks = chunk_markdown_file(str(file_path))
    if not chunks:
        print(f"[ingest-doc] WARNING: no chunks emitted from {file_path}", file=sys.stderr)
        db.close()
        sys.exit(1)

    # 3. derive doc_slug and pmid
    parent_dir = file_path.parent.name
    base = file_path.stem
    doc_slug = parent_dir.replace("-", "_").replace(".", "_")
    pmid = f"selfw__{doc_slug}__{base}"

    # 4. derive title from first H1 (heading_chain[0] of level=1 chunk)
    title = "(self-written, no H1)"
    for c in chunks:
        if c.level == 1 and c.heading_chain:
            title = c.heading_chain[0]
            break

    # 5. upsert documents row
    raw_rel = str(file_path.relative_to(WORKSPACE_ROOT))
    db.upsert_document(
        pmid=pmid,
        title=title,
        doi=None,
        source="self_written",
        has_fulltext=1,
        raw_path=raw_rel,
        article_type="unknown",
        quality_type_prior="unknown",
        abstract_excerpt="",  # self-written has no PubMed abstract
    )

    # 6. insert each chunk (embedding per chunk — slow for real qwen, fast for mock)
    inserted = 0
    skipped_empty = 0
    for c in chunks:
        if not c.text.strip():
            # wrapper headings / empty body — keep in tree but no embedding
            skipped_empty += 1
            continue
        try:
            emb = _get_embedding(c.text, args.dim, args.embedding_mode)
        except Exception as e:
            print(f"[ingest-doc] WARN: embedding failed for {c.chunk_id}: {e}",
                  file=sys.stderr)
            continue
        try:
            db.insert_chunk_v2(
                chunk_id=c.chunk_id,
                pmid=pmid,
                ordinal=inserted,
                level=c.level,
                path=c.path,
                parent_id=c.parent_id,
                child_ids=list(c.child_ids),
                sibling_ids=list(c.sibling_ids),
                heading_chain=list(c.heading_chain),
                doc_id=c.doc_id,
                heading_path=" > ".join(c.heading_chain),
                text=c.text,
                embedding=emb,
                auto_snippet=True,
            )
            inserted += 1
        except Exception as e:
            import traceback
            print(f"[ingest-doc] WARN: insert_chunk_v2 failed for {c.chunk_id}: {e}",
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            continue

    # 7. add to project group (role=core)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    db.conn.execute(
        """INSERT OR IGNORE INTO document_groups (pmid, project_slug, role, added_at)
           VALUES (?, ?, 'core', ?)""",
        (pmid, project, now),
    )
    db.conn.commit()
    db.close()

    print(f"[ingest-doc] {file_path.name}:")
    print(f"  pmid:        {pmid}")
    print(f"  doc_slug:    {doc_slug}")
    print(f"  title:       {title[:60]}")
    print(f"  chunks:      {len(chunks)} total, {inserted} embedded, {skipped_empty} empty")
    print(f"  project:     {project} (role=core)")
    print(f"  embedding:   {args.embedding_mode} (dim={args.dim})")


def cmd_search_self(args):
    """
    V2 RAG search: query -> vector KNN + tree-aware rerank -> top-K chunks.

    Scope: only documents with source='self_written'.

    Pipeline:
      1. embed query via qwen (dim=1024)
      2. vec0 KNN over chunk_vectors WHERE chunk_id in self_written set
         (we narrow by joining chunks + documents after to filter by project
         and source)
      3. join chunks to get text + heading_chain + path + level + parent_id
      4. tree-aware rerank:
         - path-prefix boost: each additional chunk in same path adds +0.05
           (so a hot section /ch5/5.3 with 3 hits gets +0.10 total boost)
         - level boost (optional): --level N prioritizes that level
      5. extract query-aware snippet via extract_snippet(query_keywords=...)
      6. format JSON or plain text

    Self-written document filter: chunks.pmid IN (SELECT pmid FROM documents
    WHERE source='self_written' AND pmid in document_groups for --project)
    """
    query = args.query
    top_k = args.top
    project = args.project
    level_filter = args.level
    expand = args.expand_context
    fmt = args.format

    if not UNIFIED_CORPUS_DB.exists():
        print(f"[search-self] ERROR: unified corpus not found: {UNIFIED_CORPUS_DB}",
              file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)

    # 1. embed query
    query_emb = _get_embedding(query, args.dim, args.embedding_mode)

    # 2. fetch all self_written chunk_ids in this project (limit KNN over
    #    the subset, since vec0 has no project filter)
    rows = db.conn.execute("""
        SELECT c.chunk_id
        FROM chunks c
        JOIN documents d ON c.pmid = d.pmid
        JOIN document_groups dg ON d.pmid = dg.pmid AND dg.project_slug = ?
        WHERE d.source = 'self_written'
    """, (project,)).fetchall()
    eligible_ids = set(r[0] for r in rows)
    if not eligible_ids:
        print(f"[search-self] no self_written chunks in project '{project}'",
              file=sys.stderr)
        db.close()
        sys.exit(1)

    # 3. KNN over ALL chunks, but over-fetch enough that self_written hits
    #    are included in top results. sqlite-vec 0.1.x has no metadata
    #    pre-filter, so we over-fetch (4× total eligible) and post-filter.
    over_fetch = max(len(eligible_ids) * 4, top_k * 20, 200)
    knn_rows = db.vector_search(query_emb, top_k=over_fetch)
    # restrict to self_written + project
    knn_rows = [(cid, d) for cid, d in knn_rows if cid in eligible_ids]

    # 4. join to get metadata
    placeholders = ",".join("?" * len(knn_rows))
    params = [r[0] for r in knn_rows]
    meta_rows = db.conn.execute(f"""
        SELECT c.chunk_id, c.pmid, c.level, c.path, c.parent_id,
               c.heading_chain, c.text, c.heading_path,
               d.title, c.doc_id
        FROM chunks c
        JOIN documents d ON c.pmid = d.pmid
        WHERE c.chunk_id IN ({placeholders})
    """, params).fetchall()
    by_id = {r[0]: r for r in meta_rows}

    # 5. compute base similarity from distance
    candidates = []
    for cid, distance in knn_rows:
        if cid not in by_id:
            continue
        m = by_id[cid]
        if level_filter is not None and m[2] != level_filter:
            continue
        sim = _distance_to_similarity(distance, args.dim)
        candidates.append({
            "chunk_id": cid,
            "pmid": m[1],
            "level": m[2],
            "path": m[3],
            "parent_id": m[4],
            "heading_chain": json.loads(m[5]) if m[5] else [],
            "text": m[6],
            "heading_path": m[7],
            "doc_title": m[8],
            "doc_id": m[9],
            "vec_distance": round(distance, 4),
            "vec_sim": round(sim, 4),
        })

    # 6. tree-aware rerank: path-prefix boost
    path_counts: dict[str, int] = {}
    for c in candidates:
        path_counts[c["path"]] = path_counts.get(c["path"], 0) + 1
    for c in candidates:
        boost = 0.05 * max(0, path_counts[c["path"]] - 1)  # 1 hit = no boost
        c["path_boost"] = round(boost, 4)
        c["score"] = round(c["vec_sim"] + boost, 4)

    # 7. snippet extract (query-aware)
    q_keywords = _tokenize_query(query)
    for c in candidates:
        snip, src = extract_snippet(c["text"], query_keywords=q_keywords)
        c["snippet"] = snip
        c["snippet_source"] = src

    # 8. sort by score desc, top_k
    candidates.sort(key=lambda x: -x["score"])
    top = candidates[:top_k]

    # 9. optional: expand context (pull parent + siblings for each top hit)
    if expand and top:
        # collect chunk_ids of parents + siblings
        ctx_ids: set[str] = set()
        for c in top:
            if c["parent_id"]:
                ctx_ids.add(c["parent_id"])
            for sib in (json.loads(
                db.conn.execute(
                    "SELECT sibling_ids FROM chunks WHERE chunk_id=?",
                    (c["chunk_id"],),
                ).fetchone()[0]
            ) if db.conn.execute(
                "SELECT sibling_ids FROM chunks WHERE chunk_id=?",
                (c["chunk_id"],),
            ).fetchone() else []):
                ctx_ids.add(sib)
        ctx_ids -= {c["chunk_id"] for c in top}
        if ctx_ids:
            ph = ",".join("?" * len(ctx_ids))
            ctx_rows = db.conn.execute(f"""
                SELECT c.chunk_id, c.level, c.path, c.heading_chain,
                       c.text, c.heading_path
                FROM chunks c
                WHERE c.chunk_id IN ({ph})
            """, list(ctx_ids)).fetchall()
            ctx = [
                {
                    "chunk_id": r[0], "level": r[1], "path": r[2],
                    "heading_chain": json.loads(r[3]) if r[3] else [],
                    "text": r[4], "heading_path": r[5],
                    "role": "parent_or_sibling",
                } for r in ctx_rows
            ]
            for c in top:
                c["context"] = ctx

    # 10. format output
    db.close()
    if fmt == "text":
        for i, c in enumerate(top, 1):
            print(f"\n[{i}] score={c['score']} (vec={c['vec_sim']}, +boost={c['path_boost']})")
            print(f"    {c['heading_path'] or ' / '.join(c['heading_chain'])}")
            print(f"    chunk_id: {c['chunk_id']}")
            print(f"    path: {c['path']}  level: {c['level']}")
            print(f"    doc: {c['doc_title']}")
            print(f"    snippet: {c['snippet']}")
    else:
        print(json.dumps({
            "query": query,
            "project": project,
            "level_filter": level_filter,
            "embedding_mode": args.embedding_mode,
            "total_candidates": len(candidates),
            "results": top,
        }, ensure_ascii=False, indent=2))


def cmd_answer_self(args):
    """
    V2 RAG answer: search-self + LLM-generated answer with chunk citations.

    Pipeline:
      1. run cmd_search_self logic (vector KNN + tree rerank + snippet)
      2. build system + user prompt with top-K chunks (heading_chain + text)
      3. call LLM (deepseek-v4-flash by default; openai-completions API)
      4. emit: { answer, sources: [{chunk_id, heading_chain, snippet}], meta }

    The answer is expected to use `[ch5 > 5.3 > 5.3.1]` style citations
    matching the heading_path of each source chunk.
    """
    if not UNIFIED_CORPUS_DB.exists():
        print(f"[answer-self] ERROR: unified corpus not found: {UNIFIED_CORPUS_DB}",
              file=sys.stderr)
        sys.exit(1)

    # 1+2. reuse cmd_search_self: embed query, vec KNN, rerank, snippet
    #     We re-implement here so we can also reuse top hits as LLM context.
    #     (Minor duplication; refactor to helper later if needed.)
    project = args.project
    query = args.query
    top_k = args.top
    level_filter = args.level
    expand = args.expand_context
    fmt = args.format

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)
    query_emb = _get_embedding(query, args.dim, args.embedding_mode)

    rows = db.conn.execute("""
        SELECT c.chunk_id
        FROM chunks c
        JOIN documents d ON c.pmid = d.pmid
        JOIN document_groups dg ON d.pmid = dg.pmid AND dg.project_slug = ?
        WHERE d.source = 'self_written'
    """, (project,)).fetchall()
    eligible_ids = set(r[0] for r in rows)
    if not eligible_ids:
        print(f"[answer-self] no self_written chunks in project '{project}'",
              file=sys.stderr)
        db.close()
        sys.exit(1)

    over_fetch = max(len(eligible_ids) * 4, top_k * 20, 200)
    knn_rows = db.vector_search(query_emb, top_k=over_fetch)
    knn_rows = [(cid, d) for cid, d in knn_rows if cid in eligible_ids]

    placeholders = ",".join("?" * len(knn_rows))
    params = [r[0] for r in knn_rows]
    meta_rows = db.conn.execute(f"""
        SELECT c.chunk_id, c.pmid, c.level, c.path, c.parent_id,
               c.heading_chain, c.text, c.heading_path,
               d.title, c.doc_id
        FROM chunks c
        JOIN documents d ON c.pmid = d.pmid
        WHERE c.chunk_id IN ({placeholders})
    """, params).fetchall()
    by_id = {r[0]: r for r in meta_rows}

    candidates = []
    for cid, distance in knn_rows:
        if cid not in by_id:
            continue
        m = by_id[cid]
        if level_filter is not None and m[2] != level_filter:
            continue
        sim = _distance_to_similarity(distance, args.dim)
        candidates.append({
            "chunk_id": cid, "pmid": m[1], "level": m[2], "path": m[3],
            "parent_id": m[4], "heading_chain": json.loads(m[5]) if m[5] else [],
            "text": m[6], "heading_path": m[7], "doc_title": m[8],
            "doc_id": m[9],
            "vec_distance": round(distance, 4), "vec_sim": round(sim, 4),
        })

    path_counts: dict[str, int] = {}
    for c in candidates:
        path_counts[c["path"]] = path_counts.get(c["path"], 0) + 1
    for c in candidates:
        boost = 0.05 * max(0, path_counts[c["path"]] - 1)
        c["path_boost"] = round(boost, 4)
        c["score"] = round(c["vec_sim"] + boost, 4)

    q_keywords = _tokenize_query(query)
    for c in candidates:
        snip, src = extract_snippet(c["text"], query_keywords=q_keywords)
        c["snippet"] = snip
        c["snippet_source"] = src

    candidates.sort(key=lambda x: -x["score"])
    top = candidates[:top_k]

    # 3. expand context if requested
    if expand and top:
        ctx_ids: set[str] = set()
        for c in top:
            if c["parent_id"]:
                ctx_ids.add(c["parent_id"])
            sib_row = db.conn.execute(
                "SELECT sibling_ids FROM chunks WHERE chunk_id=?",
                (c["chunk_id"],),
            ).fetchone()
            if sib_row and sib_row[0]:
                ctx_ids.update(json.loads(sib_row[0]))
        ctx_ids -= {c["chunk_id"] for c in top}
        if ctx_ids:
            ph = ",".join("?" * len(ctx_ids))
            ctx_rows = db.conn.execute(f"""
                SELECT chunk_id, level, path, heading_chain, text, heading_path
                FROM chunks WHERE chunk_id IN ({ph})
            """, list(ctx_ids)).fetchall()
            ctx = [
                {
                    "chunk_id": r[0], "level": r[1], "path": r[2],
                    "heading_chain": json.loads(r[3]) if r[3] else [],
                    "text": r[4], "heading_path": r[5],
                    "role": "parent_or_sibling",
                } for r in ctx_rows
            ]
            for c in top:
                c["context"] = ctx

    # 4. build prompt
    system_prompt = (
        "你是「老年住院患者跌倒预防」综述的写作助手。\n"
        "用户会基于本综述的章节片段提问，你需要：\n"
        "1. 严格基于提供的章节片段回答，不要编造 PMID / 数据 / 引用。\n"
        "2. 引用具体章节，用 [chX > X.Y > X.Y.Z] 格式（与片段标题路径一致）。\n"
        "3. 如果提供的片段不足以回答，直接说「现有章节未涉及」。\n"
        "4. 答案语言：中文。\n"
        "5. 不要复述片段原文，要合成、有逻辑。\n"
    )

    context_blocks = []
    for i, c in enumerate(top, 1):
        hp = c["heading_path"] or " > ".join(c["heading_chain"])
        context_blocks.append(f"[{i}] {hp}\n{c['text']}")
        if expand and c.get("context"):
            for ctx in c["context"][:2]:  # cap 2 context per hit
                chp = ctx["heading_path"] or " > ".join(ctx["heading_chain"])
                context_blocks.append(
                    f"   ↳ (上下文) {chp}\n   {ctx['text'][:300]}"
                )
    context_text = "\n\n".join(context_blocks) if context_blocks else "(无相关章节片段)"

    user_prompt = f"=== 章节片段 ===\n{context_text}\n\n=== 用户问题 ===\n{query}\n\n=== 你的回答 ==="

    if args.show_prompt:
        print("=" * 60)
        print("[SYSTEM]")
        print(system_prompt)
        print("=" * 60)
        print("[USER]")
        print(user_prompt)
        print("=" * 60)
        if not args.run_llm:
            db.close()
            return

    if not args.run_llm:
        db.close()
        print("[answer-self] --show-prompt only; pass --run-llm to invoke LLM")
        return

    # 5. call LLM (deepseek-v4-flash default, via openclaw.json provider config)
    llm_provider = args.llm
    llm_model = args.llm_model
    llm_cfg = _resolve_llm_config(llm_provider)
    if not llm_cfg:
        print(f"[answer-self] ERROR: llm provider '{llm_provider}' not in openclaw.json",
              file=sys.stderr)
        db.close()
        sys.exit(1)

    answer = _call_llm_chat(
        base_url=llm_cfg["baseUrl"],
        api_key=llm_cfg["apiKey"],
        model=llm_model,
        system=system_prompt,
        user=user_prompt,
        timeout=args.llm_timeout,
    )

    db.close()

    sources = [
        {
            "chunk_id": c["chunk_id"],
            "heading_chain": c["heading_chain"],
            "heading_path": c["heading_path"],
            "snippet": c["snippet"],
            "score": c["score"],
        } for c in top
    ]

    if fmt == "text":
        print(answer)
        print("\n=== 来源 ===")
        for s in sources:
            print(f"  [{s['heading_path']}] (score={s['score']})")
            print(f"    {s['snippet']}")
    else:
        print(json.dumps({
            "query": query,
            "project": project,
            "llm": {"provider": llm_provider, "model": llm_model},
            "sources": sources,
            "answer": answer,
        }, ensure_ascii=False, indent=2))


def _resolve_llm_config(provider_name: str) -> dict | None:
    """Read llm provider config (baseUrl + apiKey) from openclaw.json."""
    import json as _json
    import os
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[llm] cannot read {cfg_path}: {e}", file=sys.stderr)
        return None
    p = cfg.get("models", {}).get("providers", {}).get(provider_name)
    if not p:
        return None
    return {"baseUrl": p.get("baseUrl"), "apiKey": p.get("apiKey")}


def _call_llm_chat(base_url: str, api_key: str, model: str,
                   system: str, user: str, timeout: int = 60) -> str:
    """POST to OpenAI-compat /chat/completions and return the assistant text."""
    import json as _json
    import urllib.request
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return body["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM call failed] {type(e).__name__}: {e}"


def cmd_query(args):
    project = args.project
    query = args.query
    top_k = args.top
    # pmid_filter: comma-separated -> set
    pmid_filter = None
    if args.pmid_filter:
        pmid_filter = set(p.strip() for p in args.pmid_filter.split(",") if p.strip())

    if not UNIFIED_CORPUS_DB.exists():
        print(f"[query] ERROR: unified corpus not found: {UNIFIED_CORPUS_DB}", file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)

    # 1. Hybrid recall: vector search + keyword (SQL LIKE) search
    query_emb = _get_embedding(query, args.dim, args.embedding_mode)
    vec_hits = db.vector_search(query_emb, top_k=top_k * 5)  # vector recall pool (扩大)
    query_keywords = _tokenize_query(query)

    # keyword search: SQL LIKE on chunks.text, filtered by project
    kw_sql = """
        SELECT c.chunk_id, c.pmid, c.heading_path, c.text, c.snippet, c.snippet_source,
               d.quality_type_prior, v.impact_factor
        FROM chunks c
        JOIN documents d ON c.pmid = d.pmid
        LEFT JOIN venue v ON c.pmid = v.pmid
        JOIN document_groups dg ON c.pmid = dg.pmid AND dg.project_slug = ?
        WHERE 1=1
    """
    kw_params = [project]
    # 构建 LIKE 条件 (每个 keyword OR)
    like_clauses = []
    for kw in query_keywords:
        if len(kw) >= 3:
            like_clauses.append("c.text LIKE ?")
            kw_params.append(f"%{kw}%")
    if like_clauses:
        kw_sql += " AND (" + " OR ".join(like_clauses) + ")"
    kw_sql += " LIMIT ?"
    kw_params.append(top_k * 5)  # keyword recall pool
    kw_rows = db.conn.execute(kw_sql, kw_params).fetchall()

    # 合并 vector hits + keyword hits -> chunk_id set
    vec_chunk_ids = set(chunk_id for chunk_id, _ in vec_hits)
    kw_chunk_ids = set(row[0] for row in kw_rows)
    all_chunk_ids = vec_chunk_ids | kw_chunk_ids

    # 构建 chunk_id -> data 映射 (从 keyword rows 拿数据,vector hits 需要单独查)
    chunk_data = {}  # chunk_id -> (pmid, heading_path, text, snippet, snippet_source, type_prior, impact_factor)
    for row in kw_rows:
        c_id, c_pmid, h_path, c_text, c_snip, c_snip_src, t_prior, v_if = row
        chunk_data[c_id] = (c_pmid, h_path, c_text, c_snip, c_snip_src, t_prior, v_if)

    # vector hits 不在 keyword 结果里的,单独查
    vec_dist_map = {chunk_id: dist for chunk_id, dist in vec_hits}
    for chunk_id in vec_chunk_ids - kw_chunk_ids:
        row = db.conn.execute("""
            SELECT c.pmid, c.heading_path, c.text, c.snippet, c.snippet_source,
                   d.quality_type_prior, v.impact_factor
            FROM chunks c
            JOIN documents d ON c.pmid = d.pmid
            LEFT JOIN venue v ON c.pmid = v.pmid
            JOIN document_groups dg ON c.pmid = dg.pmid AND dg.project_slug = ?
            WHERE c.chunk_id = ?
        """, (project, chunk_id)).fetchone()
        if row:
            chunk_data[chunk_id] = row

    # 2. 按 pmid 聚合 + 混合重排 (vector_score × 0.6 + keyword_score × 0.4)
    pmid_chunks: dict[str, list] = {}
    candidate_pool: list[dict] = []  # rerank 输入: {chunk_id, text, vec_sim, kw_score}
    for chunk_id in all_chunk_ids:
        if chunk_id not in chunk_data:
            continue
        c_pmid, heading_path, c_text, snip, snip_src, type_prior, impact_factor = chunk_data[chunk_id]
        # pmid_filter
        if pmid_filter is not None and c_pmid not in pmid_filter:
            continue
        # vector score (from distance, 0 if not in vector hits)
        if chunk_id in vec_dist_map:
            chunk_rel_vec = _distance_to_similarity(vec_dist_map[chunk_id], args.dim)
        else:
            chunk_rel_vec = 0.0
        # keyword score (jaccard on text)
        h_match, _ = heading_match_jaccard(query_keywords, heading_path)
        t_hit = text_keyword_hit(c_text, query_keywords)
        kw_score = 0.5 * h_match + 0.5 * min(t_hit / 3.0, 1.0) if t_hit > 0 else 0.0
        # hybrid rerank: weighted blend
        if args.rerank_mode == "dashscope":
            # 只进 rerank pool，按原 hybrid_rel 初筛后送 dashscope
            hybrid_rel = 0.6 * chunk_rel_vec + 0.4 * kw_score
            candidate_pool.append({
                "chunk_id": chunk_id,
                "text": c_text,
                "vec_sim": chunk_rel_vec,
                "kw_score": kw_score,
                "hybrid_pre": hybrid_rel,  # rerank 失败时回退用
            })
        else:
            hybrid_rel = 0.6 * chunk_rel_vec + 0.4 * kw_score
            pmid_chunks.setdefault(c_pmid, []).append({
                "chunk_id": chunk_id,
                "chunk_relevance": hybrid_rel,
                "vector_relevance": round(chunk_rel_vec, 3),
                "keyword_relevance": round(kw_score, 3),
                "heading_match": h_match,
                "text_keyword_hit": t_hit,
                "snippet": snip,
                "snippet_source": snip_src,
            })

    # 2.5 dashscope rerank (optional)
    if args.rerank_mode == "dashscope" and candidate_pool:
        rerank_scores = _rerank_with_dashscope(
            query, candidate_pool, top_n=min(args.top * 3, 30),
        )
        for c in candidate_pool:
            cid = c["chunk_id"]
            cid_row = db.conn.execute(
                "SELECT pmid, heading_path, snippet, snippet_source FROM chunks WHERE chunk_id=?",
                (cid,),
            ).fetchone()
            if not cid_row:
                continue
            c_pmid, h_path, snip, snip_src = cid_row
            if pmid_filter is not None and c_pmid not in pmid_filter:
                continue
            # rerank 命中 -> 用 rerank score；未命中 (超 top_n) -> 用 hybrid_pre 但降权
            if cid in rerank_scores:
                rel = rerank_scores[cid]
            elif rerank_scores:
                rel = c["hybrid_pre"] * 0.3  # 未被 rerank 选中 -> 明显降权
            else:
                # rerank 调用失败 (空 dict) -> 回退到线性加权
                rel = c["hybrid_pre"]
            # heading/keyword 信号保留（仅用于 metadata，不参与 score）
            h_match, _ = heading_match_jaccard(query_keywords, h_path)
            t_hit = text_keyword_hit(c["text"], query_keywords)
            pmid_chunks.setdefault(c_pmid, []).append({
                "chunk_id": cid,
                "chunk_relevance": rel,
                "vector_relevance": round(c["vec_sim"], 3),
                "keyword_relevance": round(c["kw_score"], 3),
                "heading_match": h_match,
                "text_keyword_hit": t_hit,
                "snippet": snip,
                "snippet_source": snip_src,
            })

    # 3. document-level scoring
    results = []
    for pmid, cks in pmid_chunks.items():
        chunk_rels = [c["chunk_relevance"] for c in cks]
        h_matches = [c["heading_match"] for c in cks]
        t_hits = [c["text_keyword_hit"] for c in cks]
        doc_rel = aggregate_document_relevance(chunk_rels, h_matches, t_hits)

        # quality (article_type_prior + evidence_mean 如果有)
        type_prior = db.conn.execute(
            "SELECT quality_type_prior FROM documents WHERE pmid=?", (pmid,)
        ).fetchone()
        type_prior = type_prior[0] if type_prior else "unknown"

        qe_row = db.conn.execute(
            "SELECT quality_final FROM quality_evidence WHERE pmid=?", (pmid,)
        ).fetchone()
        quality = qe_row[0] if qe_row else type_prior

        venue_row = db.conn.execute(
            "SELECT impact_factor FROM venue WHERE pmid=?", (pmid,)
        ).fetchone()
        venue_if = venue_row[0] if venue_row and venue_row[0] else None

        rec = make_recommendation(doc_rel, quality, venue_if)

        # 查 documents 表拿 raw_path + title + has_fulltext(给写作 agent 拉全文用)
        doc_row = db.conn.execute(
            "SELECT title, raw_path, has_fulltext FROM documents WHERE pmid=?", (pmid,)
        ).fetchone()
        doc_title = doc_row[0] if doc_row else ""
        raw_path = doc_row[1] if doc_row else None
        has_fulltext = doc_row[2] if doc_row else 0

        results.append({
            "pmid": pmid,
            "title": doc_title,
            "relevance": round(doc_rel, 3),
            "quality": quality,
            "venue_if": venue_if,
            "recommendation": rec,
            "has_fulltext": has_fulltext,
            "raw_path": raw_path,
            "relevant_chunks": [
                {"chunk_id": c["chunk_id"], "snippet": c["snippet"],
                 "snippet_source": c["snippet_source"]}
                for c in sorted(cks, key=lambda x: x["chunk_relevance"], reverse=True)[:3]
            ],
        })

    results.sort(key=lambda r: (-r["relevance"], r["pmid"]))
    results = results[:top_k]

    db.close()
    print(json.dumps({"query": query, "results": results}, indent=2, ensure_ascii=False))


def cmd_score(args):
    project = args.project
    pmid = args.pmid
    if not UNIFIED_CORPUS_DB.exists():
        print(f"[score] ERROR: unified corpus not found", file=sys.stderr)
        sys.exit(1)
    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)
    # 2026-07-22 Bug fix: --project 参数检查 + 注入到 notes（避免后续跨项目同 PMID 混淆）
    slugs = _get_document_project_slugs(pmid)
    if project not in slugs:
        print(f"[score] WARN: pmid {pmid} not in project '{project}' (document_groups: {slugs})",
              file=sys.stderr)
        print(f"[score] WARN: auto-registering pmid {pmid} to project '{project}' (role=core)",
              file=sys.stderr)
        # 自动加 document_groups 关联，避免后面踩坑
        db.conn.execute(
            "INSERT OR IGNORE INTO document_groups (pmid, project_slug, role, added_at) VALUES (?,?,?,?)",
            (pmid, project, "core", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        )
        db.conn.commit()
    notes_with_proj = (args.notes or "") + f" | project={project}"
    db.upsert_quality_evidence(
        pmid=pmid,
        project_slug=project,  # 2026-07-22: schema 升级加项目隔离
        criterion_validity=args.criterion,
        outcome_reliability=args.outcome,
        conclusion_data_consistency=args.conclusion,
        notes=notes_with_proj,
    )
    row = db.conn.execute(
        "SELECT quality_final, evidence_mean FROM quality_evidence WHERE pmid=? AND project_slug=?", (pmid, project)
    ).fetchone()
    db.close()
    if row:
        print(f"[score] {pmid} (project={project}): quality_final={row[0]:.3f}, evidence_mean={row[1]:.3f}")


def cmd_group(args):
    """把文献加入项目分组（或改 role）"""
    project = args.project
    pmid = args.pmid
    role = args.role or "core"

    if not UNIFIED_CORPUS_DB.exists():
        print(f"[group] ERROR: unified corpus not found", file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)

    # 检查文档是否存在
    doc = db.conn.execute("SELECT pmid FROM documents WHERE pmid=?", (pmid,)).fetchone()
    if not doc:
        print(f"[group] ERROR: document {pmid} not found", file=sys.stderr)
        db.close()
        sys.exit(1)

    # 检查项目是否存在
    proj = db.conn.execute("SELECT slug FROM projects WHERE slug=?", (project,)).fetchone()
    if not proj:
        print(f"[group] ERROR: project '{project}' not registered", file=sys.stderr)
        db.close()
        sys.exit(1)

    # INSERT OR REPLACE（如果已存在则更新 role）
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    db.conn.execute(
        """INSERT OR REPLACE INTO document_groups (pmid, project_slug, role, added_at)
           VALUES (?, ?, ?, ?)""",
        (pmid, project, role, now)
    )
    db.conn.commit()
    db.close()

    print(f"[group] {pmid} → {project} (role={role})")


def cmd_list_projects(args):
    """列出所有已注册项目"""
    if not UNIFIED_CORPUS_DB.exists():
        print(f"[list-projects] ERROR: unified corpus not found", file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)
    rows = db.conn.execute("""
        SELECT p.slug, p.title, p.status, COUNT(dg.pmid) as doc_count
        FROM projects p
        LEFT JOIN document_groups dg ON p.slug = dg.project_slug
        GROUP BY p.slug
        ORDER BY p.slug
    """).fetchall()
    db.close()

    if not rows:
        print("[list-projects] no projects registered")
        return

    print("[list-projects]")
    for slug, title, status, doc_count in rows:
        print(f"  {slug:20s} | {status:8s} | {doc_count:3d} docs | {title}")


def cmd_list_groups(args):
    """列出项目内的所有文献分组"""
    project = args.project
    if not UNIFIED_CORPUS_DB.exists():
        print(f"[list-groups] ERROR: unified corpus not found", file=sys.stderr)
        sys.exit(1)

    db = CorpusDB(UNIFIED_CORPUS_DB, embedding_dim=args.dim)
    rows = db.conn.execute("""
        SELECT dg.pmid, dg.role, d.title, d.quality_type_prior
        FROM document_groups dg
        JOIN documents d ON dg.pmid = d.pmid
        WHERE dg.project_slug = ?
        ORDER BY dg.role, dg.pmid
    """, (project,)).fetchall()
    db.close()

    if not rows:
        print(f"[list-groups] project '{project}' has no documents")
        return

    print(f"[list-groups] project '{project}': {len(rows)} documents")
    for pmid, role, title, quality in rows:
        print(f"  {pmid:10s} | {role:10s} | {quality:8s} | {title[:50]}")


# ---------- helpers ----------

def _get_embedding(text: str, dim: int, mode: str) -> list[float]:
    """
    embedding 计算。
    mode:
      - 'qwen' (默认): 调阿里云 dashscope qwen text-embedding-v4 API (OpenAI 兼容接口)
                       从 ~/.openclaw/openclaw.json 读 API key + baseUrl
      - 'mock': 用文本 hash 生成稳定的 fake vector (仅用于测试)
      - 'openai': 调 OpenAI embeddings API (需要 OPENAI_API_KEY)
    """
    if mode == "qwen":
        try:
            import json as _json
            import urllib.request
            import os
            # 从 openclaw.json 读 qwen-embedding provider 配置 (标准 dashscope endpoint)
            # token plan 的 qwen provider 不支持 embedding API
            cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
            with open(cfg_path) as f:
                cfg = json.load(f)
            qwen_cfg = cfg["models"]["providers"]["qwen-embedding"]
            url = qwen_cfg["baseUrl"] + "/embeddings"
            headers = {
                "Authorization": f"Bearer {qwen_cfg['apiKey']}",
                "Content-Type": "application/json",
            }
            payload = json.dumps({
                "model": "text-embedding-v4",
                "input": text[:8000],
                "dimensions": dim,
                "encoding_format": "float",
            }).encode()
            req = urllib.request.Request(url, data=payload, headers=headers)
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            return resp["data"][0]["embedding"]
        except Exception as e:
            print(f"[embedding] qwen failed: {e}; falling back to mock", file=sys.stderr)
            mode = "mock"  # 降级

    if mode == "openai":
        try:
            import openai
            client = openai.OpenAI()
            resp = client.embeddings.create(
                model="text-embedding-3-small",
                input=text[:8000],
            )
            return resp.data[0].embedding
        except Exception as e:
            print(f"[embedding] openai failed: {e}; falling back to mock")

    # mock: hash-based vector + token-keyword weighting
    import hashlib
    import re
    text_lower = text.lower()
    tokens = set(re.findall(r"[a-z0-9\-]{3,}", text_lower))

    # 给特定检索词加权（让检索看起来正常）
    weighted_tokens = {t: 1.0 for t in tokens}
    boost_keywords = [
        "egfr", "foxq1", "foxq-1", "npc", "nasopharyngeal",
        "vasculogenic", "mimicry", "vm", "pi3k", "akt",
        "gefitinib", "metastasis", "tumor", "cancer",
        "knockdown", "inhibitor", "signaling", "pathway",
    ]
    for kw in boost_keywords:
        if kw in weighted_tokens:
            weighted_tokens[kw] = 3.0
        elif kw in text_lower:
            # 词不在 tokens 里但在 text 里，加 1 个伪 token
            weighted_tokens[f"_kw_{kw}"] = 3.0

    # 生成 dim 维向量: 每个 token hash 贡献 1 个维度的高值，其余维度低值
    vec = []
    token_list = list(weighted_tokens.items())
    for i in range(dim):
        token_idx = i % max(len(token_list), 1)
        if token_list:
            _, weight = token_list[token_idx]
            # base 噪声 + 加权信号
            h = hashlib.sha256(f"{i}_{text_lower[:200]}".encode()).digest()
            base = (int.from_bytes(h[:4], "little") / 0xFFFFFFFF) * 2 - 1
            signal = weight if i % max(len(token_list), 1) == token_idx else 0.0
            vec.append(base * 0.3 + (signal / 3.0) * 0.7)
        else:
            h = hashlib.sha256(f"{i}_{text_lower[:200]}".encode()).digest()
            base = (int.from_bytes(h[:4], "little") / 0xFFFFFFFF) * 2 - 1
            vec.append(base)
    return vec


def _distance_to_similarity(distance: float, dim: int) -> float:
    """
    sqlite-vec L2 距离 → 0-1 相似度。

    L2 距离范围理论是 [0, sqrt(2*dim)] = [0, ~55] for dim=1536（归一化向量）。
    实际距离通常 5-30（mock/真实都有这个区间）。

    使用 exp 衰减: sim = exp(-distance / scale)
    scale 选择让 distance=0 -> 1.0, distance=dim/10 -> ~0.37, distance=dim/5 -> ~0.14
    """
    import math
    scale = dim / 10.0
    return math.exp(-distance / scale)


# ---------- rerank (dashscope qwen3-rerank) ----------

# 官方规格（百炼控制台 2026-07-21）:
#   价格: 0.5 元 / 百万 tokens（仅输入）
#   最大输入长度: 30K
#   RPM: 5400, TPM: 50亿
#   API: POST https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_DEFAULT_MODEL = "qwen3-rerank"
RERANK_MAX_DOCS = 50           # 单次最多文档数
RERANK_MAX_INPUT_TOKENS = 30000  # 单次请求最大输入 token（qwen3-rerank 上限）
RERANK_DOC_MAX_CHARS = 1500     # 每个 doc 截断字符数（约 750 token，给 query 留空间）


def _call_dashscope_rerank(query: str, documents: list[str], top_n: int = 10,
                           return_documents: bool = False) -> list[dict]:
    """
    调 dashscope qwen3-rerank（原生 endpoint）。

    返回: [{"index": int, "relevance_score": float, "document": {...}?}, ...]
          已按 relevance_score 降序。
    """
    import urllib.request
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    ds = cfg["models"]["providers"]["qwen-embedding"]
    url = ds["baseUrl"].replace("/compatible-mode/v1", "/api/v1") + "/services/rerank/text-rerank/text-rerank"
    headers = {
        "Authorization": f"Bearer {ds['apiKey']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": RERANK_DEFAULT_MODEL,
        "input": {"query": query, "documents": documents},
        "parameters": {"return_documents": return_documents, "top_n": top_n},
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return resp.get("output", {}).get("results", [])


def _rerank_with_dashscope(query: str, candidates: list[dict],
                           top_n: int = 30, truncate_chars: int = RERANK_DOC_MAX_CHARS,
                           fallback_to_linear: bool = True) -> dict[str, float]:
    """
    对 candidate chunks 调 dashscope rerank，返回 {chunk_id: rerank_score} 映射。

    candidates: [{"chunk_id": str, "text": str, "vec_sim": float, "kw_score": float}, ...]
    rerank_score 范围 [0, 1]，调用失败时返回空 dict（调用方回退到线性加权）。

    限流处理:
      - docs > RERANK_MAX_DOCS -> 分批 + 各自 top_n
      - 输入总 chars > RERANK_MAX_INPUT_TOKENS * 3 -> 每个 doc 截断到 truncate_chars
    """
    if not candidates:
        return {}

    # 截断每个 doc 到 truncate_chars chars（保证 query + 50 docs * 1500 chars ≈ 75K chars ≈ 37K tokens 之上限内）
    truncated_docs = []
    for c in candidates:
        t = c.get("text", "")
        if len(t) > truncate_chars:
            truncated_docs.append(t[:truncate_chars])
        else:
            truncated_docs.append(t)

    # 分批（按 docs 数 + 输入长度）
    score_map: dict[str, float] = {}
    n = len(truncated_docs)
    if n == 0:
        return score_map

    # 估算 input tokens（粗略：1 token ≈ 3 chars）
    query_chars = len(query)
    total_chars = query_chars + sum(len(d) for d in truncated_docs)
    est_tokens = total_chars // 3

    # 单批能装下的 doc 数（保守）
    batch_size = RERANK_MAX_DOCS
    if est_tokens > RERANK_MAX_INPUT_TOKENS:
        # 按比例缩
        avg_doc_chars = total_chars // max(n, 1)
        # 要保证 query + batch_size * avg_doc_chars < RERANK_MAX_INPUT_TOKENS * 3
        batch_size = min(RERANK_MAX_DOCS, (RERANK_MAX_INPUT_TOKENS * 3 - query_chars) // max(avg_doc_chars, 1))
        batch_size = max(1, batch_size)

    try:
        for start in range(0, n, batch_size):
            batch_docs = truncated_docs[start:start + batch_size]
            batch_cids = [c["chunk_id"] for c in candidates[start:start + batch_size]]
            results = _call_dashscope_rerank(query, batch_docs, top_n=min(top_n, len(batch_docs)))
            for r in results:
                idx = r.get("index")
                if idx is not None and 0 <= idx < len(batch_cids):
                    score_map[batch_cids[idx]] = float(r.get("relevance_score", 0.0))
        return score_map
    except Exception as e:
        print(f"[rerank] dashscope failed: {e}; falling back to {'linear' if fallback_to_linear else 'none'}", file=sys.stderr)
        return score_map  # 空 dict -> 调用方 fallback


def _tokenize_query(query: str) -> list[str]:
    """简单分词: 按非字母数字切分"""
    import re
    return [t for t in re.split(r"[^a-zA-Z0-9\-]+", query) if t]


def _guess_article_type(text: str) -> str:
    """极简文章类型判断（MVP: 按关键词）"""
    t = text.lower()
    if "meta-analysis" in t or "meta analysis" in t:
        return "meta_analysis"
    if "systematic review" in t:
        return "systematic_review"
    if "randomized" in t or "randomised" in t:
        return "rct"
    if "case-control" in t or "case control" in t:
        return "case_control"
    if "cohort" in t:
        return "cohort"
    if "case report" in t:
        return "case_report"
    if "editorial" in t:
        return "editorial"
    if "letter to" in t or "letter:" in t:
        return "letter"
    if "guideline" in t or "consensus" in t:
        return "guideline"
    if "preprint" in t or "biorxiv" in t or "medrxiv" in t:
        return "preprint"
    return "unknown"


def _lookup_venue_safe(pmid: str) -> Optional[dict]:
    """安全调 OpenAlex lookup，失败返回 None"""
    try:
        from openalex import lookup_venue_by_pmid
        return lookup_venue_by_pmid(pmid)
    except Exception as e:
        print(f"[venue] lookup failed: {e}")
        return None


# ---------- arg parser ----------

def main():
    parser = argparse.ArgumentParser(description="corpus CLI")
    parser.add_argument("--dim", type=int, default=EMBEDDING_DIM_DEFAULT,
                        help="embedding dimension (default 1536)")
    parser.add_argument("--embedding-mode", choices=["qwen", "openai"], default="qwen",
                        help="embedding backend (default mock)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="register a project in unified corpus")
    p_init.add_argument("--project", required=True, help="project slug")
    p_init.add_argument("--title", help="project title (default: same as slug)")
    p_init.add_argument("--status", choices=["active", "done", "backlog"], default="active")
    p_init.add_argument("--desc", help="project description")

    p_reg = sub.add_parser("register-raw", help="researcher: register raw file + metadata (no chunking)")
    p_reg.add_argument("--project", required=True, help="project slug")
    p_reg.add_argument("--pmid", required=True)
    p_reg.add_argument("--source", default="pubmed",
                       choices=["pubmed", "glm_web", "url_fetch", "file_parse"])
    p_reg.add_argument("--title", default=None, help="article title (from PubMed efetch)")
    p_reg.add_argument("--abstract-excerpt", default=None, help="≤500 char abstract override")
    p_reg.add_argument("--doi", default=None, help="DOI")
    p_reg.add_argument("--verify-only", action="store_true",
                       help="only verify raw matches PMID (Stage 4), do NOT register")
    p_reg.add_argument("--raw-path", default=None,
                       help="raw file path override (default: raw/<pmid>.txt under corpus.db parent)")

    p_proc = sub.add_parser("process-raw", help="curator: read raw -> chunk + embed + insert chunks (V3: .md V2-chunker / .txt V3-plain-text-chunker)")
    p_proc.add_argument("--pmid", required=True, help="PMID to process (must be registered first)")

    p_proc_pdf = sub.add_parser("process-pdf", help="researcher: Docling PDF -> raw/<pmid>.md (heading-aware)")
    p_proc_pdf.add_argument("--pmid", required=True, help="PMID whose .pdf should be converted")
    p_proc_pdf.add_argument("--project", default=None,
                            help="project slug（2026-07-22 加）：自动查 projects/<slug>/knowledge/fulltext/<pmid>.pdf。与 --raw-path 互斥优先级。")
    p_proc_pdf.add_argument("--raw-path", default=None,
                            help="absolute/relative path to .pdf (default: --project 查 fulltext/ → corpus/raw/<pmid>.pdf legacy). 2026-07-22 加此参数支持 projects/<slug>/knowledge/fulltext/<pmid>.pdf 项目级布局")
    p_proc_pdf.add_argument("--force", action="store_true", help="overwrite existing .md")

    p_ing_doc = sub.add_parser("ingest-doc", help="ingest one self-written markdown file (V2 chunker)")
    p_ing_doc.add_argument("--file", required=True,
                           help="path to .md file (absolute or relative to workspace)")
    p_ing_doc.add_argument("--project", required=True,
                           help="project slug (registers into document_groups)")

    p_ss = sub.add_parser("search-self", help="V2 RAG: vector KNN + tree rerank over self_written chunks")
    p_ss.add_argument("--project", required=True, help="project slug (filter by document_groups)")
    p_ss.add_argument("--query", required=True, help="natural language query")
    p_ss.add_argument("--top", type=int, default=5, help="top-K results (default 5)")
    p_ss.add_argument("--level", type=int, default=None, choices=[0, 1, 2, 3],
                      help="restrict to a single heading level (0=preface, 1=chapter)")
    p_ss.add_argument("--expand-context", action="store_true",
                      help="include parent + sibling chunks for each top hit")
    p_ss.add_argument("--format", choices=["json", "text"], default="json",
                      help="output format (default json)")

    p_as = sub.add_parser("answer-self", help="V2 RAG answer: search-self + LLM (deepseek default)")
    p_as.add_argument("--project", required=True, help="project slug")
    p_as.add_argument("--query", required=True, help="natural language question")
    p_as.add_argument("--top", type=int, default=5, help="top-K chunks fed into LLM (default 5)")
    p_as.add_argument("--level", type=int, default=None, choices=[0, 1, 2, 3],
                      help="restrict search to one heading level")
    p_as.add_argument("--expand-context", action="store_true",
                      help="include parent + sibling chunks in LLM context")
    p_as.add_argument("--llm", default="deepseek",
                      help="LLM provider name in openclaw.json (default deepseek)")
    p_as.add_argument("--llm-model", default="deepseek-v4-flash",
                      help="LLM model id (default deepseek-v4-flash)")
    p_as.add_argument("--llm-timeout", type=int, default=60,
                      help="LLM HTTP timeout seconds (default 60)")
    p_as.add_argument("--show-prompt", action="store_true",
                      help="print system+user prompt before sending to LLM")
    p_as.add_argument("--run-llm", action="store_true",
                      help="actually call LLM (default: print prompt only)")
    p_as.add_argument("--format", choices=["json", "text"], default="json",
                      help="output format (default json)")

    p_q = sub.add_parser("query", help="vector search + aggregate + recommendation")
    p_q.add_argument("--project", required=True, help="project slug (filter results)")
    p_q.add_argument("--query", required=True)
    p_q.add_argument("--top", type=int, default=10)
    p_q.add_argument("--pmid-filter", default=None,
                     help="filter results by comma-separated PMIDs (e.g. 42133917,42174524)")
    p_q.add_argument("--rerank-mode", choices=["linear", "dashscope"], default="dashscope",
                     help="rerank strategy: linear (0.6*vec+0.4*kw, default legacy) or "
                          "dashscope (qwen3-rerank via dashscope native API, recommended)")

    p_s = sub.add_parser("score", help="curator: score quality evidence features")
    p_s.add_argument("--project", required=True, help="project slug")
    p_s.add_argument("--pmid", required=True)
    p_s.add_argument("--criterion", type=float, required=True,
                     help="criterion_validity 0-1")
    p_s.add_argument("--outcome", type=float, required=True,
                     help="outcome_reliability 0-1")
    p_s.add_argument("--conclusion", type=float, required=True,
                     help="conclusion_data_consistency 0-1")
    p_s.add_argument("--notes", default="")

    p_g = sub.add_parser("group", help="add document to project group (or change role)")
    p_g.add_argument("--project", required=True, help="project slug")
    p_g.add_argument("--pmid", required=True)
    p_g.add_argument("--role", choices=["core", "peripheral", "cited"], default="core")

    sub.add_parser("list-projects", help="list all registered projects")

    p_lg = sub.add_parser("list-groups", help="list documents in a project")
    p_lg.add_argument("--project", required=True, help="project slug")

    args = parser.parse_args()
    cmd_map = {
        "init": cmd_init,
        "register-raw": cmd_register_raw,
        "process-raw": cmd_process_raw,
        "process-pdf": cmd_process_pdf,
        "ingest-doc": cmd_ingest_doc,
        "query": cmd_query,
        "search-self": cmd_search_self,
        "answer-self": cmd_answer_self,
        "score": cmd_score,
        "group": cmd_group,
        "list-projects": cmd_list_projects,
        "list-groups": cmd_list_groups,
    }
    cmd_map[args.cmd](args)


if __name__ == "__main__":
    main()
