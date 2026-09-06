"""Contract tests for corpus.generation (IMPLEMENTATION.md §2).

Covers: length-prefixed v2 chunk-id round-trip with special ``_``/``__``/``/``/``:``
in the path, collision distinctness, legacy id parse + write rejection, the
generation manifest DDL (partial unique index rejecting two active per pmid),
and the re-chunk state machine (activate switches old→retired keeping one active;
a rolled-back switch keeps the old active; orphan building cleanup).
"""

from __future__ import annotations

import sqlite3

import pytest

import generation as G


# --------------------------------------------------------------------------- #
# chunk_id codec
# --------------------------------------------------------------------------- #

CK = G.chunker8("markdown_v2", "2.3.1")          # a real 8-hex fingerprint
HASH = "sha256:" + "ab12cd34ef56" + "0" * 52     # first 12 hex = ab12cd34ef56


def test_chunk_id_roundtrip_plain():
    s = G.encode_chunk_id(generation=42, content_hash=HASH, chunker_fp=CK,
                          n=2, path="1/2", part="p1")
    cid = G.parse_chunk_id(s)
    assert cid.legacy is False
    assert cid.generation == 42
    assert cid.content_hash12 == "ab12cd34ef56"
    assert cid.chunker8 == CK
    assert cid.n == 2
    assert cid.path == "1/2"
    assert cid.part == "p1"
    assert G.encode_chunk_id(generation=cid.generation, content_hash=cid.content_hash12,
                             chunker_fp=cid.chunker8, n=cid.n, path=cid.path,
                             part=cid.part) == s


@pytest.mark.parametrize("path", [
    "1/2",
    "ch5/5.1/5.1.1",
    "a__b__c",                 # double underscore has no special meaning
    "weird_seg/with:colon",    # colon inside path — length prefix makes it inert
    "trailing__",
    "",                        # empty path
    "5:1/2:3",                 # the exact colon-laden path from the contract example
])
def test_chunk_id_roundtrip_special_chars(path):
    s = G.encode_chunk_id(generation=1, content_hash=HASH, chunker_fp=CK,
                          n=7, path=path, part="p3")
    cid = G.parse_chunk_id(s)
    assert cid.path == path
    assert cid.part == "p3"
    # re-encode is byte-identical → no ambiguity from the special characters
    assert G.encode_chunk_id(generation=1, content_hash=HASH, chunker_fp=CK,
                             n=7, path=path, part="p3") == s


def test_chunk_id_no_collision_between_distinct_paths():
    # "a__b" (one path) vs "a/b" vs "a_b" must all encode distinctly and parse back.
    ids = {
        p: G.parse_chunk_id(
            G.encode_chunk_id(generation=1, content_hash=HASH, chunker_fp=CK,
                              n=1, path=p, part="p1")
        ).path
        for p in ("a__b", "a/b", "a_b")
    }
    assert ids == {"a__b": "a__b", "a/b": "a/b", "a_b": "a_b"}
    encoded = {
        G.encode_chunk_id(generation=1, content_hash=HASH, chunker_fp=CK, n=1, path=p, part="p1")
        for p in ("a__b", "a/b", "a_b")
    }
    assert len(encoded) == 3        # three distinct wire strings, no collision


def test_contract_example_parses():
    # The literal §2 example shape (hex chunker8 per the NEW_RE, path "1/2:3").
    s = f"c2.g000001a.ab12cd34ef56.{CK}.2:5:1/2:3:p1"
    cid = G.parse_chunk_id(s)
    assert cid.generation == G._b36_decode("g000001a")
    assert cid.path == "1/2:3"
    assert cid.part == "p1"


def test_legacy_id_parse_and_write_rejected():
    s = "review_ai_fall_elderly__ch5__5.1__5.1.1__p1"
    cid = G.parse_chunk_id(s)
    assert cid.legacy is True
    assert cid.generation is None
    assert cid.part == "p1"
    assert cid.legacy_pmid == "review_ai_fall_elderly"
    with pytest.raises(G.LegacyChunkIdNotWritable):
        G.require_writable(cid)


def test_require_writable_allows_v2():
    cid = G.parse_chunk_id(
        G.encode_chunk_id(generation=1, content_hash=HASH, chunker_fp=CK, n=1, path="x", part="p1")
    )
    assert G.require_writable(cid) is cid


@pytest.mark.parametrize("bad", [
    "not_a_chunk_id",
    "c2.BADGEN00.ab12cd34ef56.deadbeef.1:1:x:p1",     # gen8 uppercase → NEW_RE fails
    "c2.g0000001.zzzzzzzzzzzz.deadbeef.1:1:x:p1",      # hash12 not hex
    "c2.g0000001.ab12cd34ef56.deadbeef.1:9:x:p1",      # path length overruns
    "c2.g0000001.ab12cd34ef56.deadbeef.1:1:xy:p1",     # path not ':' terminated
])
def test_invalid_chunk_ids_rejected(bad):
    with pytest.raises(G.InvalidChunkId):
        G.parse_chunk_id(bad)


# --------------------------------------------------------------------------- #
# generation manifest + state machine
# --------------------------------------------------------------------------- #

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None          # autocommit — explicit BEGIN IMMEDIATE
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE documents(pmid TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, pmid TEXT)")
    G.install_generation_schema(conn)
    return conn


def test_two_active_rejected_by_unique_index():
    conn = _conn()
    now = "2026-09-06T00:00:00Z"
    conn.execute(
        "INSERT INTO generation_manifest(generation,pmid,content_hash,chunker_name,"
        "chunker_version,status,created_at) VALUES (1,'p1','h','ck','1','active',?)", (now,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO generation_manifest(generation,pmid,content_hash,chunker_name,"
            "chunker_version,status,created_at) VALUES (2,'p1','h','ck','1','active',?)", (now,))


def test_activate_switches_old_to_retired_and_updates_document():
    conn = _conn()
    conn.execute("INSERT INTO documents(pmid,title) VALUES ('p1','t')")
    g1 = G.begin_generation(conn, pmid="p1", content_hash="h1", chunker_name="ck", chunker_version="1")
    G.activate_generation(conn, g1, chunk_count=10)
    assert G.active_generation_for(conn, "p1") == g1
    assert conn.execute("SELECT generation FROM documents WHERE pmid='p1'").fetchone()[0] == g1

    g2 = G.begin_generation(conn, pmid="p1", content_hash="h2", chunker_name="ck", chunker_version="2")
    G.activate_generation(conn, g2, chunk_count=12)
    assert G.active_generation_for(conn, "p1") == g2
    assert conn.execute("SELECT status FROM generation_manifest WHERE generation=?", (g1,)).fetchone()[0] == "retired"
    assert conn.execute("SELECT generation FROM documents WHERE pmid='p1'").fetchone()[0] == g2
    # exactly one active for the pmid at all times
    n_active = conn.execute(
        "SELECT COUNT(*) FROM generation_manifest WHERE pmid='p1' AND status='active'").fetchone()[0]
    assert n_active == 1


def test_switch_rollback_keeps_old_active():
    # Simulate a crash mid-switch: run the retire+activate SQL then ROLLBACK.
    # The old generation must remain the sole active one.
    conn = _conn()
    conn.execute("INSERT INTO documents(pmid,title) VALUES ('p1','t')")
    g1 = G.begin_generation(conn, pmid="p1", content_hash="h1", chunker_name="ck", chunker_version="1")
    G.activate_generation(conn, g1)
    g2 = G.begin_generation(conn, pmid="p1", content_hash="h2", chunker_name="ck", chunker_version="2")

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE generation_manifest SET status='retired' WHERE generation=?", (g1,))
    conn.execute("UPDATE generation_manifest SET status='active' WHERE generation=?", (g2,))
    conn.execute("ROLLBACK")

    assert G.active_generation_for(conn, "p1") == g1
    assert conn.execute("SELECT status FROM generation_manifest WHERE generation=?", (g2,)).fetchone()[0] == "building"


def test_fail_generation_marks_failed():
    conn = _conn()
    g1 = G.begin_generation(conn, pmid="p1", content_hash="h1", chunker_name="ck", chunker_version="1")
    G.fail_generation(conn, g1, error="embed failed")
    row = conn.execute("SELECT status,error FROM generation_manifest WHERE generation=?", (g1,)).fetchone()
    assert row == ("failed", "embed failed")


def test_cleanup_orphan_building():
    conn = _conn()
    g1 = G.begin_generation(conn, pmid="p1", content_hash="h1", chunker_name="ck", chunker_version="1")
    # g1 has no chunks → orphan; a second building gen WITH a chunk is preserved.
    g2 = G.begin_generation(conn, pmid="p2", content_hash="h2", chunker_name="ck", chunker_version="1")
    conn.execute("INSERT INTO chunks(chunk_id,pmid,generation) VALUES ('c1','p2',?)", (g2,))
    orphans = G.cleanup_orphan_building(conn)
    assert orphans == [g1]
    assert conn.execute("SELECT status FROM generation_manifest WHERE generation=?", (g1,)).fetchone()[0] == "failed"
    assert conn.execute("SELECT status FROM generation_manifest WHERE generation=?", (g2,)).fetchone()[0] == "building"
