"""
corpus.db — SQLite + sqlite-vec 向量数据库 schema 和工具

每个项目一个 corpus 实例:
  projects/<project>/corpus/metadata.sqlite

5 张表:
  - documents      文献元信息（pmid/title/doi/source/article_type/quality_type_prior）
  - venue          期刊 venue 信息（IF/JCR/CAS，可 unknown）
  - chunks         chunk 文本 + heading_path + embedding (vec0)
  - relevance      chunk 级相关度评分（待 curator 重算）
  - quality_evidence  curator 重算的证据核验特征（3 项 + 综合分）

所有字段都有默认值；section_importance 等预留字段 default=1.0 避免脏数据。
"""

from __future__ import annotations
import datetime
import hashlib
import json as _json_top
import sqlite3
import sqlite_vec
import struct
import time
from pathlib import Path
from typing import Iterable, Optional

import generation as _gen


# ---------- vector helpers ----------

def serialize_float32(vector: list[float]) -> bytes:
    """sqlite-vec 期望 little-endian float32 字节流"""
    return struct.pack(f"<{len(vector)}f", *vector)


def deserialize_float32(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ---------- schema ----------

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- vec0 虚拟表（sqlite-vec）存 chunk embedding
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding float[%d]              -- dimension 占位，运行时替换
);

CREATE TABLE IF NOT EXISTS documents (
    pmid TEXT PRIMARY KEY,
    title TEXT,
    doi TEXT,
    source TEXT,                      -- pubmed | glm_web | url_fetch | file_parse
    retrieved_at TEXT,
    has_fulltext INTEGER DEFAULT 0,  -- 0/1 是否有 raw 全文
    raw_path TEXT,                    -- 指向 raw/<pmid>.txt
    article_type TEXT,                -- meta_analysis | rct | cohort | case_control | case_report | editorial | preprint | unknown
    quality_type_prior TEXT,          -- high | medium | low | unknown
    abstract_excerpt TEXT,            -- 截取的摘要（≤ 500 字）
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS venue (
    pmid TEXT PRIMARY KEY REFERENCES documents(pmid) ON DELETE CASCADE,
    journal TEXT,
    year INTEGER,
    impact_factor REAL,               -- 允许 NULL（unknown）
    jcr_quartile TEXT,                -- Q1/Q2/Q3/Q4/unknown
    cas_zone TEXT,                    -- 一区/二区/三区/四区/unknown
    source TEXT                       -- openalex | manual | unknown
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    pmid TEXT NOT NULL REFERENCES documents(pmid) ON DELETE CASCADE,
    ordinal INTEGER,                  -- 在原文章中的顺序
    heading_path TEXT,                -- breadcrumb, e.g. "Introduction > EGFR signaling"
    section_importance REAL DEFAULT 1.0,  -- MVP 占位，v1 实现动态权重
    text TEXT,
    snippet TEXT,                     -- 语义位置的摘要（见 extract_snippet，MVP=chunk 第一句）
    snippet_source TEXT,              -- first_sentence | keyword_window | fallback_short
    char_start INTEGER,
    char_end INTEGER,
    created_at TEXT,
    -- V2 tree fields (mirrors migrate_v2.sql so a fresh db matches a migrated one)
    level INTEGER DEFAULT 0,
    path TEXT,
    parent_id TEXT,
    child_ids TEXT,                   -- JSON array string
    sibling_ids TEXT,                 -- JSON array string
    heading_chain TEXT,               -- JSON array string
    doc_id TEXT,
    generation INTEGER                -- generation_manifest.generation (NULL only for legacy)
);

CREATE INDEX IF NOT EXISTS idx_chunks_pmid ON chunks(pmid);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS relevance (
    chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    query TEXT,                       -- 触发的 query
    chunk_relevance REAL DEFAULT 0.0, -- 0-1
    heading_match INTEGER DEFAULT 0,  -- 0/1 Jaccard + 关键词命中
    jaccard REAL DEFAULT 0.0,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS quality_evidence (
    pmid TEXT PRIMARY KEY REFERENCES documents(pmid) ON DELETE CASCADE,
    criterion_validity REAL,          -- 0-1
    outcome_reliability REAL,
    conclusion_data_consistency REAL,
    evidence_mean REAL,               -- mean of 3 features
    quality_final REAL,               -- max(quality_type_prior_score, evidence_mean)
    computed_at TEXT,
    notes TEXT                        -- curator 自由备注
);
"""


# ---------- quality v3 schema (IMPLEMENTATION.md §3) ----------
#
# Append-only evidence history (quality_evidence_v3) + a deterministically
# selected current pointer (quality_current). v1/v2 tables coexist untouched.

QUALITY_V3_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quality_evidence_v3(
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
CREATE INDEX IF NOT EXISTS quality_v3_lookup
 ON quality_evidence_v3(pmid,project,formula_version,rubric_version,scored_at,evidence_id);
CREATE TABLE IF NOT EXISTS quality_current(
 pmid TEXT NOT NULL, project TEXT NOT NULL, formula_version TEXT NOT NULL, rubric_version TEXT NOT NULL,
 evidence_id INTEGER NOT NULL UNIQUE REFERENCES quality_evidence_v3(evidence_id),
 selected_at TEXT NOT NULL, selection_reason TEXT NOT NULL,
 PRIMARY KEY(pmid,project,formula_version,rubric_version)
);
"""

QUALITY_STATUS_ENUM = ("prior_only", "partial", "scored", "override")


ARTICLE_TYPE_TO_PRIOR = {
    "meta_analysis": "high",
    "systematic_review": "high",
    "rct": "high",
    "cohort": "medium",
    "case_control": "medium",
    "case_series": "medium",
    "case_report": "low",
    "editorial": "low",
    "letter": "low",
    "preprint": "medium",          # 待评估
    "guideline": "high",           # 指南本身证据级别最高
    "unknown": "unknown",
}

QUALITY_TO_NUMERIC = {"high": 0.85, "medium": 0.55, "low": 0.25, "unknown": 0.40}


# ---------- connection ----------

class CorpusDB:
    def __init__(self, db_path: str | Path, embedding_dim: int = 1536):
        self.db_path = Path(db_path)
        self.embedding_dim = embedding_dim
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        # Autocommit: the generation state machine and the quality-v3 writer manage
        # their own explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` transactions, which
        # requires sqlite3's implicit transaction manager to be out of the way.
        self.conn.isolation_level = None
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._init_schema()

    def _init_schema(self):
        sql = SCHEMA_SQL % self.embedding_dim
        self.conn.executescript(sql)
        # generation manifest + active-per-pmid unique index + generation columns
        _gen.install_generation_schema(self.conn)
        # append-only quality history + deterministic current pointer
        self.conn.executescript(QUALITY_V3_SCHEMA_SQL)
        self._documents_has_project = self._has_column("documents", "project")

    def _has_column(self, table: str, column: str) -> bool:
        return any(r[1] == column for r in self.conn.execute(f"PRAGMA table_info({table})"))

    def close(self):
        self.conn.close()

    # ---------- documents ----------

    def upsert_document(self, pmid: str, **fields):
        fields.setdefault("created_at", _now_iso())
        cols = list(fields.keys())
        placeholders = ",".join("?" * len(cols))
        col_list = ",".join(cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "pmid")
        sql = f"""
        INSERT INTO documents (pmid, {col_list})
        VALUES (?, {placeholders})
        ON CONFLICT(pmid) DO UPDATE SET {updates}
        """
        self.conn.execute(sql, [pmid, *fields.values()])
        self.conn.commit()

    def get_document(self, pmid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE pmid=?", (pmid,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.execute("SELECT * FROM documents LIMIT 1").description]
        return dict(zip(cols, row))

    # ---------- venue ----------

    def upsert_venue(self, pmid: str, **fields):
        cols = list(fields.keys())
        placeholders = ",".join("?" * len(cols))
        col_list = ",".join(cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "pmid")
        sql = f"""
        INSERT INTO venue (pmid, {col_list})
        VALUES (?, {placeholders})
        ON CONFLICT(pmid) DO UPDATE SET {updates}
        """
        self.conn.execute(sql, [pmid, *fields.values()])
        self.conn.commit()

    def get_venue(self, pmid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM venue WHERE pmid=?", (pmid,)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.execute("SELECT * FROM venue LIMIT 1").description]
        return dict(zip(cols, row))

    # ---------- chunks + vectors ----------

    def insert_chunk(self, chunk_id: str, pmid: str, text: str, embedding: list[float],
                     heading_path: str = "", snippet: str = "", snippet_source: str = "fallback_short",
                     ordinal: int = 0,
                     char_start: int = 0, char_end: int = 0,
                     section_importance: float = 1.0,
                     generation: Optional[int] = None,
                     auto_snippet: bool = True,
                     query_keywords: list[str] | None = None):
        """
        插入一个 chunk. 默认 (auto_snippet=True) 会调用 extract_snippet
        从 text 里自动生成 snippet 和 snippet_source；调用方可传入 override.

        vec0 写入用 DELETE+INSERT（不用 INSERT OR REPLACE：vec0 的 REPLACE 语义
        会在虚拟表内留下悬挂 rowid，见 §3）。chunks + chunk_vectors 在一个显式
        事务内写入以保持一致。
        """
        if auto_snippet and not snippet:
            snippet, snippet_source = extract_snippet(text, query_keywords=query_keywords)
        if len(embedding) != self.embedding_dim:
            raise ValueError(
                f"embedding dim {len(embedding)} != schema dim {self.embedding_dim}"
            )
        now = _now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("""
            INSERT OR REPLACE INTO chunks
                (chunk_id, pmid, ordinal, heading_path, section_importance,
                 text, snippet, snippet_source, char_start, char_end, created_at, generation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chunk_id, pmid, ordinal, heading_path, section_importance,
                  text, snippet, snippet_source, char_start, char_end, now, generation))
            # vec0 表：DELETE 再 INSERT（替代 INSERT OR REPLACE）
            self.conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,))
            self.conn.execute("""
            INSERT INTO chunk_vectors (chunk_id, embedding)
            VALUES (?, ?)
            """, (chunk_id, serialize_float32(embedding)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def insert_chunk_v2(self, chunk_id: str, pmid: str, text: str, embedding: list[float],
                        ordinal: int = 0,
                        # V2 tree fields:
                        level: int = 0, path: str = "",
                        parent_id: Optional[str] = None,
                        child_ids: list[str] | None = None,
                        sibling_ids: list[str] | None = None,
                        heading_chain: list[str] | None = None,
                        doc_id: str = "",
                        # legacy/compat:
                        heading_path: str = "",
                        snippet: str = "", snippet_source: str = "fallback_short",
                        char_start: int = 0, char_end: int = 0,
                        section_importance: float = 1.0,
                        generation: Optional[int] = None,
                        auto_snippet: bool = True):
        """V2 chunk insert with tree fields (level / path / parent_id / child_ids /
        sibling_ids / heading_chain / doc_id).

        Writes both `chunks` table AND `chunk_vectors` vec0 in one transaction.
        vec0 write is DELETE+INSERT (not INSERT OR REPLACE) per §3.
        """
        if auto_snippet and not snippet:
            snippet, snippet_source = extract_snippet(text)
        if len(embedding) != self.embedding_dim:
            raise ValueError(
                f"embedding dim {len(embedding)} != schema dim {self.embedding_dim}"
            )
        import json as _json
        child_ids_json = _json.dumps(child_ids or [], ensure_ascii=False)
        sibling_ids_json = _json.dumps(sibling_ids or [], ensure_ascii=False)
        heading_chain_json = _json.dumps(heading_chain or [], ensure_ascii=False)
        now = _now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("""
            INSERT OR REPLACE INTO chunks
                (chunk_id, pmid, ordinal, heading_path, section_importance,
                 text, snippet, snippet_source, char_start, char_end, created_at,
                 level, path, parent_id, child_ids, sibling_ids, heading_chain, doc_id, generation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chunk_id, pmid, ordinal, heading_path, section_importance,
                  text, snippet, snippet_source, char_start, char_end, now,
                  level, path, parent_id, child_ids_json, sibling_ids_json,
                  heading_chain_json, doc_id, generation))
            self.conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,))
            self.conn.execute("""
            INSERT INTO chunk_vectors (chunk_id, embedding)
            VALUES (?, ?)
            """, (chunk_id, serialize_float32(embedding)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get_chunks_for_pmid(self, pmid: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE pmid=? ORDER BY ordinal", (pmid,)
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM chunks LIMIT 1").description]
        return [dict(zip(cols, r)) for r in rows]

    # ---------- query (vector search) ----------

    def vector_search(self, query_embedding: list[float], *,
                      project: Optional[str] = None,
                      pmid_filter: Optional[str] = None,
                      generation: Optional[int] = None,
                      top_k: int = 10,
                      _include_retired: bool = False, **kwargs) -> list[tuple[str, float]]:
        """返回 (chunk_id, distance) 列表；distance 越小越相关。

        §2/§3 契约：所有 KNN 强制 active generation。vec0 MATCH 在子查询里独立
        执行（vec0 只允许向量列参与 MATCH 谓词），然后立即 JOIN chunks 与
        generation_manifest（status='active'），再做 project/pmid/generation 过滤。

        - 未传 generation：JOIN 上的 ``g.status='active'`` 已经把每个 pmid 限制在
          其 active generation。
        - 显式 generation：只能是 active（``include_retired=True`` 为离线审计放开，
          返回 retired 代）。
        - legacy（generation IS NULL）的 chunk 永远不进 KNN（JOIN 直接排除）。
        """
        _include_retired = bool(kwargs.pop("include_retired", _include_retired))
        if kwargs: raise TypeError(f"unexpected keyword arguments: {kwargs}")
        top_k = max(1, int(top_k))
        params: list = [serialize_float32(query_embedding), top_k]
        join_status = "" if _include_retired else " AND g.status='active'"
        sql = f"""
        SELECT v.chunk_id, v.distance
        FROM (
            SELECT chunk_id, distance
            FROM chunk_vectors
            WHERE embedding MATCH ? AND k = ?
        ) v
        JOIN chunks c ON c.chunk_id = v.chunk_id
        JOIN generation_manifest g ON g.generation = c.generation{join_status}
        """
        where: list[str] = []
        if generation is not None:
            where.append("c.generation = ?")
            params.append(int(generation))
        if pmid_filter is not None:
            where.append("c.pmid = ?")
            params.append(pmid_filter)
        if project is not None and getattr(self, "_documents_has_project", False):
            where.append("c.pmid IN (SELECT pmid FROM documents WHERE project = ?)")
            params.append(project)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY v.distance"
        return self.conn.execute(sql, params).fetchall()

    # ---------- relevance (chunk → doc aggregation) ----------

    def upsert_relevance(self, chunk_id: str, query: str,
                         chunk_relevance: float, heading_match: bool,
                         jaccard: float):
        self.conn.execute("""
        INSERT OR REPLACE INTO relevance
            (chunk_id, query, chunk_relevance, heading_match, jaccard, computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (chunk_id, query, chunk_relevance, 1 if heading_match else 0, jaccard, _now_iso()))
        self.conn.commit()

    # ---------- quality_evidence (curator 重算) ----------

    def upsert_quality_evidence(self, pmid: str, project_slug: str,
                                 criterion_validity: float | None = None,
                                 outcome_reliability: float | None = None,
                                 conclusion_data_consistency: float | None = None,
                                 notes: str = ""):
        # 2026-07-22: project_slug 加入参数；PK 变 (pmid, project_slug) 支持跨项目同 PMID
        feats = [f for f in (criterion_validity, outcome_reliability, conclusion_data_consistency)
                 if f is not None]
        evidence_mean = sum(feats) / len(feats) if feats else None
        # quality_final: max(type_prior_numeric, evidence_mean)
        doc = self.get_document(pmid)
        type_prior = doc.get("quality_type_prior", "unknown") if doc else "unknown"
        type_prior_num = QUALITY_TO_NUMERIC.get(type_prior, 0.40)
        if evidence_mean is None:
            quality_final = type_prior_num
        else:
            quality_final = max(type_prior_num, evidence_mean)

        self.conn.execute("""
        INSERT OR REPLACE INTO quality_evidence
            (pmid, project_slug, criterion_validity, outcome_reliability, conclusion_data_consistency,
             evidence_mean, quality_final, computed_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pmid, project_slug, criterion_validity, outcome_reliability, conclusion_data_consistency,
              evidence_mean, quality_final, _now_iso(), notes))
        self.conn.commit()

    # ---------- quality v3 (append-only history + deterministic current) ----------

    @staticmethod
    def recompute_idempotency_key(pmid: str, project: str, formula_version: str,
                                  rubric_version: str, canonical_input_json: str,
                                  override_by: Optional[str], override_reason: Optional[str]) -> str:
        """idempotency_key = sha256(pmid|project|formula|rubric|canonical_input_json|
        override_by|override_reason)。**绝不含 scored_at** — 同一业务输入重试得到同 key。"""
        payload = "|".join((
            pmid, project, formula_version, rubric_version,
            canonical_input_json or "",
            override_by or "", override_reason or "",
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def insert_quality_v3(self, *, pmid: str, project: str,
                          formula_version: str, rubric_version: str,
                          quality_final: float, quality_status: str,
                          source: str, source_event_id: str,
                          criterion_validity: Optional[float] = None,
                          outcome_reliability: Optional[float] = None,
                          conclusion_data_consistency: Optional[float] = None,
                          prior_score: Optional[float] = None,
                          canonical_input_json: Optional[str] = None,
                          override_reason: Optional[str] = None,
                          override_by: Optional[str] = None,
                          scored_at: Optional[str] = None) -> dict:
        """Append an evidence row (idempotent on ``idempotency_key``) and re-select
        ``quality_current`` — all in one transaction.

        Returns a dict of the *effective* evidence row plus ``inserted`` (False if
        the same idempotency_key already existed → original row returned, no new row).
        """
        if quality_status not in QUALITY_STATUS_ENUM:
            raise ValueError(f"invalid quality_status: {quality_status!r}")
        if quality_status == "override":
            if not (override_reason and override_by):
                raise ValueError("override requires both override_reason and override_by")
        feats = [f for f in (criterion_validity, outcome_reliability, conclusion_data_consistency)
                 if f is not None]
        evidence_mean = sum(feats) / len(feats) if feats else None
        idem_key = self.recompute_idempotency_key(
            pmid, project, formula_version, rubric_version,
            canonical_input_json or "", override_by, override_reason,
        )
        now = _utc_now()
        scored_at = scored_at or now

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT evidence_id FROM quality_evidence_v3 WHERE idempotency_key=?",
                (idem_key,),
            ).fetchone()
            if existing is None:
                self.conn.execute("""
                INSERT INTO quality_evidence_v3
                    (pmid, project, formula_version, rubric_version,
                     criterion_validity, outcome_reliability, conclusion_data_consistency,
                     evidence_mean, prior_score, quality_final, quality_status,
                     override_reason, override_by, source, source_event_id, idempotency_key,
                     scored_at, created_at)
                VALUES (?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?)
                """, (pmid, project, formula_version, rubric_version,
                      criterion_validity, outcome_reliability, conclusion_data_consistency,
                      evidence_mean, prior_score, quality_final, quality_status,
                      override_reason, override_by, source, source_event_id, idem_key,
                      scored_at, now))
                inserted = True
            else:
                inserted = False
            # deterministically (re)select current in the same transaction
            self._select_current(pmid, project, formula_version, rubric_version, now)
            row = self._evidence_by_key(idem_key)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        row["inserted"] = inserted
        return row

    def _evidence_by_key(self, idem_key: str) -> dict:
        cur = self.conn.execute(
            "SELECT * FROM quality_evidence_v3 WHERE idempotency_key=?", (idem_key,))
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))

    def _select_current(self, pmid: str, project: str, formula_version: str,
                        rubric_version: str, now: str) -> None:
        """Pick the winning evidence row (``scored_at DESC, evidence_id DESC``) and
        explicitly upsert ``quality_current`` — never rely on runtime ordering."""
        win = self.conn.execute("""
            SELECT evidence_id FROM quality_evidence_v3
            WHERE pmid=? AND project=? AND formula_version=? AND rubric_version=?
            ORDER BY scored_at DESC, evidence_id DESC
            LIMIT 1
        """, (pmid, project, formula_version, rubric_version)).fetchone()
        if win is None:
            return
        evidence_id = win[0]
        self.conn.execute("""
            INSERT INTO quality_current
                (pmid, project, formula_version, rubric_version, evidence_id, selected_at, selection_reason)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(pmid,project,formula_version,rubric_version) DO UPDATE SET
                evidence_id=excluded.evidence_id,
                selected_at=excluded.selected_at,
                selection_reason=excluded.selection_reason
        """, (pmid, project, formula_version, rubric_version, evidence_id, now,
              "max(scored_at,evidence_id)"))

    def upsert_quality_current(self, pmid: str, project: str,
                               formula_version: str, rubric_version: str) -> Optional[dict]:
        """Standalone re-selection of ``quality_current`` for one key (used by
        migration / repair). Returns the resulting current row, or None."""
        now = _utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._select_current(pmid, project, formula_version, rubric_version, now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get_current_quality(pmid, project, formula_version, rubric_version)

    def get_current_quality(self, pmid: str, project: str,
                            formula_version: str, rubric_version: str) -> Optional[dict]:
        """The current evidence row for a key (joins current → evidence)."""
        cur = self.conn.execute("""
            SELECT e.*, qc.selected_at, qc.selection_reason
            FROM quality_current qc
            JOIN quality_evidence_v3 e ON e.evidence_id = qc.evidence_id
            WHERE qc.pmid=? AND qc.project=? AND qc.formula_version=? AND qc.rubric_version=?
        """, (pmid, project, formula_version, rubric_version))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


# ---------- utility ----------

def _now_iso() -> str:
    """ISO-8601 with timezone; uses local time"""
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _utc_now() -> str:
    """UTC timestamp ``YYYY-MM-DDTHH:MM:SSZ`` for append-only history rows."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- article_relevance aggregation (用于主 agent 端) ----------

def heading_match_jaccard(query_keywords: list[str], heading_path: str,
                          min_jaccard: float = 0.30) -> tuple[bool, float]:
    """
    Jaccard ≥ min_jaccard 且至少 1 个关键词原词命中 → True
    返回 (matched, jaccard_score)
    """
    K = _normalize_tokens(query_keywords)
    H = _normalize_tokens(heading_path.split())
    if not K or not H:
        return False, 0.0
    inter = K & H
    jaccard = len(inter) / len(K | H)
    return (jaccard >= min_jaccard and bool(inter)), jaccard


def _normalize_tokens(tokens: Iterable[str]) -> set[str]:
    """小写、去标点、英文单复数归一"""
    out = set()
    for t in tokens:
        t = t.lower().strip(".,;:()[]{}'\"")
        if not t or len(t) <= 1:
            continue
        if t.endswith("ies") and len(t) > 4:
            t = t[:-3] + "y"
        elif t.endswith("es") and len(t) > 3 and not t.endswith("oes"):
            t = t[:-2]
        elif t.endswith("s") and len(t) > 3:
            t = t[:-1]
        out.add(t)
    return out


def aggregate_document_relevance(chunk_relevances: list[float],
                                 heading_matches: list[bool],
                                 text_keyword_hits: list[bool] | None = None) -> float:
    """
    chunk → document 聚合规则 (MVP 最小化文本命中补偿版)：

      1. heading_match=True 的 chunk: 计入 max，discount=1.0
      2. heading_match=False 但 text_keyword_hit=True 的 chunk: 仍可计入 max，
         discount=1.0 (文本明确相关，heading 表达不一致不应误杀)
      3. heading_match=False 且 text_keyword_hit=False: discount=0.5 (降权保守)
      4. 若全为 3: document_relevance = max(全部 chunk relevance) * 0.5

    语义说明:
      heading_match 定位章节相关性 (辅助信号)
      text_keyword_hit 避免标题/层级表达不一致导致的误杀 (纠错兑底)
      两者都 True / 都不可用 → 仍走折扣
    """
    if not chunk_relevances:
        return 0.0
    if text_keyword_hits is None:
        # 保持向后兼容：旧调用方不传文本命中，按原行为
        text_keyword_hits = [False] * len(chunk_relevances)

    # 优先级 1+2: heading match OR text hit 都可计入
    relevant = [
        r for r, h, t in zip(chunk_relevances, heading_matches, text_keyword_hits)
        if h or t
    ]
    if relevant:
        return max(relevant)
    # 优先级 3: 全不相关，discount 50%
    return max(chunk_relevances) * 0.5


def text_keyword_hit(chunk_text: str, query_keywords: list[str]) -> bool:
    """
    chunk_text 是否直接命中 query 关键词 (大小写/词干归一化)。

    与 heading_match_jaccard 的区别:
      - heading_match_jaccard: heading_path vs query, Jaccard ≥ 0.30
      - text_keyword_hit: chunk 正文 vs query, 只要命中 1 个关键词就 True

    用途: aggregate_document_relevance 在 heading 不匹配时纠错使用。
    """
    if not query_keywords or not chunk_text:
        return False
    text_tokens = _normalize_tokens(chunk_text.split())
    query_tokens = _normalize_tokens(query_keywords)
    return bool(text_tokens & query_tokens)


def make_recommendation(relevance: float, quality: str | float,
                        venue_if: float | None) -> str:
    """
    primary:     quality ≥ medium (0.55) AND relevance ≥ 0.75
    supporting:  quality ≥ medium (0.55) AND relevance ≥ 0.50
    background:  其它有相关性的文献

    venue 不参与门槛；IF 只在同分时排序。
    """
    qn = quality if isinstance(quality, (int, float)) else QUALITY_TO_NUMERIC.get(quality, 0.40)
    if qn >= QUALITY_TO_NUMERIC["medium"] and relevance >= 0.75:
        return "primary"
    if qn >= QUALITY_TO_NUMERIC["medium"] and relevance >= 0.50:
        return "supporting"
    return "background"


# ---------- snippet extraction (MVP v0) ----------
#
# 规则 (MVP):
#   Rule 1: chunk 第一句 (first_sentence)
#   Rule 2: 关键词窗口 (keyword_window)，可选低优先级
#   Rule 3: 兜底前 target_len 字符 (fallback_short)
#
# 重要：MVP **不**使用 "heading 下第一句"——因为 chunk_text 通常不包含
# heading_path，heading_path 只用于 heading_match 判断。v1 可选扩展为
# "保留 heading 作为 chunk 前缀"，届时再升级 Rule 1。
#
# 返回 (snippet, source) 元组。source 枚举由 schema 约束:
#   first_sentence | keyword_window | fallback_short

def extract_snippet(chunk_text: str,
                    query_keywords: list[str] | None = None,
                    target_len: int = 50) -> tuple[str, str]:
    """
    MVP snippet 提取。
    返回 (snippet_text, source)。

    规则 (MVP):
      Rule 1: chunk 第一句（_first_sentence 在 target+10 范围内找最近句末）
              - 若该句太短（< 20 字符）→ 降级 Rule 2
              - 若该句太长（> target + 15）→ 降级 Rule 2（避免 trim 后仍超长）
      Rule 2: 关键词窗口（仅当 query_keywords 提供）
      Rule 3: 兜底前 target_len 字符

    v1 可扩展 Rule 1 为 "heading 下第一句"（需 chunk_text 保留 heading）。
    """
    if not chunk_text:
        return "", "fallback_short"

    # Rule 1: chunk 第一句
    first = _first_sentence(chunk_text, target=target_len)
    if first and 20 <= len(first) <= target_len + 15:
        trimmed = _trim_to_boundary(first, target_len)
        return trimmed, "first_sentence"

    # Rule 2: 关键词窗口
    if query_keywords:
        kw = _keyword_window(chunk_text, query_keywords, target_len)
        if kw:
            return kw, "keyword_window"

    # Rule 3: 兜底
    return _fallback_short(chunk_text, target_len), "fallback_short"


def _first_sentence(text: str, target: int = 50) -> str:
    """取 text 中不超过 target+10 字符范围内的第一个完整句子。

    设计要点：不是简单地"取第一个句末符之前的内容"，而是:
    1. 先扫整个文本，记录所有句末位置（.!? 后跟空格/换行/结束）
    2. 返回"不超过 target+10 字符的最近句末"
    这样 _trim_to_boundary 可以接句末边界，不会被强制 trim 到空格。

    边界处理：
    - 跳过位置 < 10 的句末（避免 'E. coli'）
    - 如果全文无句末，返回全文 (strip)
    """
    sentence_ends = []
    for i, ch in enumerate(text):
        if i < 10:
            continue
        if ch in ".!?":
            # 句末必须后跟空格/换行/字符串末尾
            if i + 1 >= len(text) or text[i + 1] in " \n\t":
                sentence_ends.append(i)
        elif ch == "\n" and i + 1 < len(text):
            # 换行也算句末（但要后面还有内容）
            sentence_ends.append(i)
    if not sentence_ends:
        return text.strip()
    # 取不超过 target+10 的最近句末
    cap = target + 10
    best = None
    for pos in sentence_ends:
        if pos + 1 <= cap:
            best = pos
        else:
            break
    if best is None:
        # 最近的句末也在 cap 之后 → 取最早的
        best = sentence_ends[0]
    return text[: best + 1].strip()


def _trim_to_boundary(text: str, target: int) -> str:
    """trim 到 target 附近，优先句末/换行，其次空格边界
    避免在半 token 处截断"""
    if len(text) <= target + 5:
        return text
    # 先找 target ±10 范围内最近的句末/换行
    for i in range(target, min(target + 10, len(text))):
        if text[i] in ".!?" and i + 1 < len(text) and text[i + 1] == " ":
            return text[: i + 1].strip()
    for i in range(target - 1, max(target - 10, 0), -1):
        if text[i] == "\n":
            return text[:i].strip()
    # 再退到空格边界
    cut = text.rfind(" ", max(target - 10, 0), min(target + 10, len(text)))
    if cut > 0:
        return text[:cut].rstrip(",;:-").strip()
    # 实在没边界就硬截
    return text[:target].rstrip(",;:-").strip() + "..."


def _keyword_window(text: str, keywords: list[str], target: int) -> str | None:
    """找第一个关键词命中位置，返回前后 ~target/2 字符
    trim 优先按句末/换行，其次空格"""
    text_lower = text.lower()
    half = target // 2
    for kw in keywords:
        if not kw:
            continue
        idx = text_lower.find(kw.lower())
        if idx < 0:
            continue
        start = max(0, idx - half)
        end = min(len(text), idx + len(kw) + half)
        window = text[start:end].strip()
        # 头尾 trim 到句末/换行/空格边界
        if start > 0:
            # 找 window 第一个空格，去掉 prefix
            sp = window.find(" ")
            if 0 < sp < len(window) - 5:
                window = "..." + window[sp + 1 :]
        if end < len(text):
            sp = window.rfind(" ")
            if sp > 0 and sp < len(window) - 3:
                window = window[:sp] + "..."
        return window
    return None


def _fallback_short(text: str, target: int) -> str:
    """兑底：前 target 字符 + 可能的 ..."""
    if len(text) <= target:
        return text
    # 找空格边界避免半词
    cut = text.rfind(" ", max(target - 10, 0), target + 5)
    if cut > 0:
        return text[:cut].rstrip(",;:-").strip() + "..."
    return text[:target].rstrip(",;:-").strip() + "..."