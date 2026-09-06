"""Contract tests for corpus.ipc (IMPLEMENTATION.md §5).

Covers: half-packet framing dispatches each frame exactly once; a frame over
1 MiB raises frame_too_large; malformed/unsupported frames are structured errors;
the idempotency cache returns the identical full response on retry (one DB row),
a duplicate score_event_id is not re-executed, request-hash conflicts are flagged,
a processing lease is only taken over once expired, and an expired cache row is
re-executed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import ipc as I


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #

def _frame(**kw) -> bytes:
    kw.setdefault("schema_version", 1)
    kw.setdefault("request_id", "r1")
    return I.encode_frame(kw)


def test_half_packet_dispatched_once():
    r = I.FrameReader()
    full = _frame(request_id="r1", command="ping")
    half = len(full) // 2
    assert r.feed(full[:half]) == []            # partial → nothing yet
    frames = r.feed(full[half:])                # completing newline → exactly one
    assert len(frames) == 1
    assert frames[0]["request_id"] == "r1"
    # feeding more after the newline does not re-emit the earlier frame
    assert r.feed(b"") == []


def test_two_frames_split_across_chunks():
    r = I.FrameReader()
    a = _frame(request_id="a")
    b = _frame(request_id="b")
    stream = a + b
    got = []
    # feed one byte at a time — each frame must appear exactly once, in order
    for i in range(len(stream)):
        got.extend(r.feed(stream[i:i + 1]))
    assert [f["request_id"] for f in got] == ["a", "b"]


def test_frame_too_large_no_newline():
    r = I.FrameReader()
    with pytest.raises(I.FrameTooLarge):
        r.feed(b"x" * (I.MAX_FRAME_BYTES + 1))


def test_encode_frame_too_large():
    big = {"schema_version": 1, "request_id": "r", "blob": "z" * (I.MAX_FRAME_BYTES + 10)}
    with pytest.raises(I.FrameTooLarge):
        I.encode_frame(big)


def test_parse_frame_structured_errors():
    with pytest.raises(I.InvalidFrame):
        I.parse_frame('{"schema_version":2,"request_id":"r"}')      # unsupported schema
    with pytest.raises(I.InvalidFrame):
        I.parse_frame('{"schema_version":1}')                        # missing request_id
    with pytest.raises(I.InvalidFrame):
        I.parse_frame("not json")


# --------------------------------------------------------------------------- #
# idempotency cache
# --------------------------------------------------------------------------- #

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    I.install_ipc_schema(conn)
    return conn


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM ipc_idempotency").fetchone()[0]


def test_retry_returns_identical_body_one_row():
    conn = _conn()
    key = I.score_db_key("proj", "p1", "f1", "r1", "evt-1")
    req = json.dumps({"pmid": "p1"})
    r1 = I.reserve(conn, key=key, request_id="req-1", command="score", request_json=req)
    assert r1.status == "new"
    body = json.dumps({"quality_final": 0.7, "evidence_id": 5})
    I.commit_response(conn, key=key, response_json=body)

    # response lost → client retries with the same key
    r2 = I.reserve(conn, key=key, request_id="req-1-retry", command="score", request_json=req)
    assert r2.status == "cached"
    assert r2.response_json == body
    assert r2.request_conflict is False
    assert _count(conn) == 1                     # exactly one DB row


def test_duplicate_score_event_id_not_re_executed():
    conn = _conn()
    key = I.score_db_key("proj", "p1", "f1", "r1", "evt-42")
    req = json.dumps({"pmid": "p1"})
    I.reserve(conn, key=key, request_id="a", command="score", request_json=req)
    I.commit_response(conn, key=key, response_json='{"ok":true}')
    again = I.reserve(conn, key=key, request_id="b", command="score", request_json=req)
    assert again.status == "cached"              # not "new" → no re-execution
    assert _count(conn) == 1


def test_request_hash_conflict_flagged():
    conn = _conn()
    key = I.score_db_key("proj", "p1", "f1", "r1", "evt-9")
    I.reserve(conn, key=key, request_id="a", command="score", request_json='{"pmid":"p1"}')
    I.commit_response(conn, key=key, response_json='{"ok":true}')
    # same key, DIFFERENT request body → cached but flagged as a conflict
    r = I.reserve(conn, key=key, request_id="b", command="score", request_json='{"pmid":"DIFFERENT"}')
    assert r.status == "cached"
    assert r.request_conflict is True


def test_processing_lease_takeover_only_when_expired():
    conn = _conn()
    key = "score:proj:p1:f1:r1:evt-lease"
    t0 = datetime(2026, 9, 6, 0, 0, 0, tzinfo=timezone.utc)
    I.reserve(conn, key=key, request_id="a", command="score", request_json="{}", now=t0)

    # within the 30s lease → in_progress (no takeover)
    within = I.reserve(conn, key=key, request_id="b", command="score", request_json="{}",
                       now=t0 + timedelta(seconds=10))
    assert within.status == "in_progress"

    # past the lease → takeover allowed
    after = I.reserve(conn, key=key, request_id="c", command="score", request_json="{}",
                      now=t0 + timedelta(seconds=31))
    assert after.status == "new"
    assert _count(conn) == 1


def test_expired_cache_re_executes():
    conn = _conn()
    key = "score:proj:p1:f1:r1:evt-ttl"
    t0 = datetime(2026, 9, 6, 0, 0, 0, tzinfo=timezone.utc)
    I.reserve(conn, key=key, request_id="a", command="score", request_json="{}",
              ttl_seconds=3600, now=t0)
    I.commit_response(conn, key=key, response_json='{"ok":true}', now=t0)
    # long after expiry → re-execute (status new), not cached
    later = I.reserve(conn, key=key, request_id="b", command="score", request_json="{}",
                      now=t0 + timedelta(seconds=7200))
    assert later.status == "new"
