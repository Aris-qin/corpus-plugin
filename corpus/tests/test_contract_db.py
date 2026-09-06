"""Contract tests for corpus.db §2/§3.

Covers: vector_search generation filtering (KNN never returns retired or legacy/
NULL-generation chunks; explicit generation must be active; include_retired opens
the audit path); quality v3 append-only history with deterministic current
selection (scored_at tie → larger evidence_id wins), idempotent retry (no double
write), override field validation, and v1/v2 table coexistence.
"""

from __future__ import annotations

import pytest

import db as D
import generation as G


def _mk_db(tmp_path):
    return D.CorpusDB(tmp_path / "t.db", embedding_dim=8)


def _emb(i: int) -> list[float]:
    v = [0.0] * 8
    v[i] = 1.0
    return v


# --------------------------------------------------------------------------- #
# vector_search generation filtering
# --------------------------------------------------------------------------- #

def _setup_generations(db):
    db.upsert_document("p1", title="doc1")
    g1 = G.begin_generation(db.conn, pmid="p1", content_hash="h1", chunker_name="ck", chunker_version="1")
    G.activate_generation(db.conn, g1)
    g2 = G.begin_generation(db.conn, pmid="p1", content_hash="h2", chunker_name="ck", chunker_version="2")
    G.activate_generation(db.conn, g2)          # g1 now retired, g2 active
    return g1, g2


def test_knn_excludes_retired_and_legacy(tmp_path):
    db = _mk_db(tmp_path)
    g_ret, g_act = _setup_generations(db)
    db.insert_chunk("c_active", "p1", "active text", _emb(0), generation=g_act)
    db.insert_chunk("c_retired", "p1", "retired text", _emb(1), generation=g_ret)
    db.insert_chunk("c_legacy", "p1", "legacy text", _emb(2), generation=None)

    # query nearest the retired vector — it must still be filtered out.
    res = db.vector_search(_emb(1), top_k=10)
    ids = {cid for cid, _ in res}
    assert "c_active" in ids
    assert "c_retired" not in ids
    assert "c_legacy" not in ids


def test_knn_include_retired_audit(tmp_path):
    db = _mk_db(tmp_path)
    g_ret, g_act = _setup_generations(db)
    db.insert_chunk("c_active", "p1", "a", _emb(0), generation=g_act)
    db.insert_chunk("c_retired", "p1", "r", _emb(1), generation=g_ret)
    res = db.vector_search(_emb(1), top_k=10, include_retired=True)
    ids = {cid for cid, _ in res}
    assert {"c_active", "c_retired"} <= ids


def test_knn_explicit_generation_must_be_active(tmp_path):
    db = _mk_db(tmp_path)
    g_ret, g_act = _setup_generations(db)
    db.insert_chunk("c_active", "p1", "a", _emb(0), generation=g_act)
    db.insert_chunk("c_retired", "p1", "r", _emb(1), generation=g_ret)
    # explicit active generation → returns it
    assert any(cid == "c_active" for cid, _ in db.vector_search(_emb(0), generation=g_act, top_k=10))
    # explicit retired generation without include_retired → nothing
    assert db.vector_search(_emb(1), generation=g_ret, top_k=10) == []


def test_knn_pmid_filter(tmp_path):
    db = _mk_db(tmp_path)
    db.upsert_document("p1", title="d1")
    db.upsert_document("p2", title="d2")
    g1 = G.begin_generation(db.conn, pmid="p1", content_hash="h", chunker_name="ck", chunker_version="1")
    G.activate_generation(db.conn, g1)
    g2 = G.begin_generation(db.conn, pmid="p2", content_hash="h", chunker_name="ck", chunker_version="1")
    G.activate_generation(db.conn, g2)
    db.insert_chunk("c1", "p1", "x", _emb(0), generation=g1)
    db.insert_chunk("c2", "p2", "y", _emb(0), generation=g2)
    ids = {cid for cid, _ in db.vector_search(_emb(0), pmid_filter="p1", top_k=10)}
    assert ids == {"c1"}


# --------------------------------------------------------------------------- #
# quality v3
# --------------------------------------------------------------------------- #

def test_current_tie_scored_at_prefers_larger_evidence_id(tmp_path):
    db = _mk_db(tmp_path)
    db.upsert_document("p1", title="d")
    ts = "2026-09-06T00:00:00Z"
    r1 = db.insert_quality_v3(pmid="p1", project="proj", formula_version="f1", rubric_version="r1",
                              quality_final=0.6, quality_status="scored", source="worker",
                              source_event_id="e1", criterion_validity=0.6,
                              canonical_input_json='{"v":1}', scored_at=ts)
    r2 = db.insert_quality_v3(pmid="p1", project="proj", formula_version="f1", rubric_version="r1",
                              quality_final=0.7, quality_status="scored", source="worker",
                              source_event_id="e2", criterion_validity=0.7,
                              canonical_input_json='{"v":2}', scored_at=ts)
    assert r2["evidence_id"] > r1["evidence_id"]
    cur = db.get_current_quality("p1", "proj", "f1", "r1")
    assert cur["evidence_id"] == r2["evidence_id"]         # tie broken by larger evidence_id


def test_idempotent_retry_no_double_write(tmp_path):
    db = _mk_db(tmp_path)
    db.upsert_document("p1", title="d")
    kwargs = dict(pmid="p1", project="proj", formula_version="f1", rubric_version="r1",
                  quality_final=0.6, quality_status="scored", source="worker",
                  source_event_id="e1", criterion_validity=0.6, canonical_input_json='{"v":1}',
                  scored_at="2026-09-06T00:00:00Z")
    a = db.insert_quality_v3(**kwargs)
    assert a["inserted"] is True
    n1 = db.conn.execute("SELECT COUNT(*) FROM quality_evidence_v3").fetchone()[0]
    b = db.insert_quality_v3(**kwargs)
    assert b["inserted"] is False
    assert b["evidence_id"] == a["evidence_id"]
    n2 = db.conn.execute("SELECT COUNT(*) FROM quality_evidence_v3").fetchone()[0]
    assert n1 == n2 == 1


def test_override_requires_reason_and_by(tmp_path):
    db = _mk_db(tmp_path)
    db.upsert_document("p1", title="d")
    with pytest.raises(ValueError):
        db.insert_quality_v3(pmid="p1", project="proj", formula_version="f1", rubric_version="r1",
                             quality_final=0.9, quality_status="override", source="human",
                             source_event_id="o1", override_reason="looks great", override_by=None)
    # complete override is accepted and becomes current
    row = db.insert_quality_v3(pmid="p1", project="proj", formula_version="f1", rubric_version="r1",
                              quality_final=0.9, quality_status="override", source="human",
                              source_event_id="o1", override_reason="curator adjust", override_by="alice")
    assert row["quality_status"] == "override"
    cur = db.get_current_quality("p1", "proj", "f1", "r1")
    assert cur["override_by"] == "alice"


def test_invalid_status_rejected(tmp_path):
    db = _mk_db(tmp_path)
    db.upsert_document("p1", title="d")
    with pytest.raises(ValueError):
        db.insert_quality_v3(pmid="p1", project="proj", formula_version="f1", rubric_version="r1",
                             quality_final=0.5, quality_status="bogus", source="w", source_event_id="e")


def test_v1_v2_quality_tables_coexist(tmp_path):
    db = _mk_db(tmp_path)
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"quality_evidence", "quality_evidence_v3", "quality_current"} <= tables


def test_idempotency_key_excludes_scored_at(tmp_path):
    db = _mk_db(tmp_path)
    k1 = db.recompute_idempotency_key("p1", "proj", "f1", "r1", '{"v":1}', None, None)
    k2 = db.recompute_idempotency_key("p1", "proj", "f1", "r1", '{"v":1}', None, None)
    assert k1 == k2       # deterministic; no scored_at component
