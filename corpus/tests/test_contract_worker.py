"""Worker socket link tests (§5): start worker → socket client query/score → JSON response.

Covers the real deployment shape: worker opens its OWN DB connection from
``db_path`` (no injected connection), single-threaded accept/serve loop, JSONL
framed requests; query over active-generation KNN returns structured results;
score writes quality v3 idempotently (replay adds no rows); health + invalid
request rejection.
"""

from __future__ import annotations

import json
import os
import socket
import threading

import pytest

import db as D
import generation as G
import worker as W
from ipc import FrameReader, encode_frame


def _mk_db(tmp_path):
    return D.CorpusDB(tmp_path / "t.db", embedding_dim=8)


def _emb(i: int) -> list[float]:
    v = [0.0] * 8
    v[i] = 1.0
    return v


@pytest.fixture
def worker_env(tmp_path):
    """Seed data via a throwaway connection, then run a real worker bound to
    ``db_path`` with an accept/serve thread (worker opens its own connection)."""
    db = _mk_db(tmp_path)
    db.upsert_document("p1", title="doc1")
    g1 = G.begin_generation(db.conn, pmid="p1", content_hash="h1", chunker_name="ck", chunker_version="1")
    G.activate_generation(db.conn, g1)
    db.insert_chunk("c1", "p1", "EGFR degradation in nasopharyngeal carcinoma",
                    _emb(0), generation=g1, heading_path="Methods/Results")
    db.insert_chunk("c2", "p1", "unrelated botanical text",
                    _emb(1), generation=g1, heading_path="Abstract")
    db.conn.close()                     # worker must open its own connection

    wk = W.Worker(pidfile=tmp_path / "w.pid", socket_path=tmp_path / "w.sock",
                  db_path=str(tmp_path / "t.db"), embedding_dim=8)
    listen_sock = wk.start()
    stop = threading.Event()

    def _serve():
        listen_sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                wk.serve_once(conn)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        yield wk, listen_sock, tmp_path / "w.sock"
    finally:
        stop.set()
        listen_sock.close()
        wk.release()
        sp = wk.socket_path
        if sp.exists():
            sp.unlink()


def _rpc(sock_path, request: dict) -> dict:
    """Client side only: connect, send one JSONL frame, read the framed reply,
    close (which ends the server's serve_once recv loop)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(sock_path))
    reply = None
    try:
        s.sendall(encode_frame(request))
        reader = FrameReader()
        while True:
            data = s.recv(65536)
            if not data:
                break
            frames = reader.feed(data)
            if frames:
                reply = frames[0]
                break
    finally:
        s.close()
    assert reply is not None, "no reply frame received"
    return reply


def test_socket_query_link(worker_env):
    """Real link: worker-owned DB, active-generation KNN, structured results."""
    _, _, sock = worker_env
    resp = _rpc(sock, {
        "schema_version": 1,
        "request_id": "req-1",
        "command": "query",
        "params": {"query": "EGFR degradation carcinoma", "embedding": _emb(0), "top_k": 5},
    })
    assert resp["ok"] is True
    res = resp["result"]["results"]
    assert len(res) >= 1
    first = res[0]
    assert first["chunk_id"] == "c1"          # nearest to _emb(0)
    assert first["pmid"] == "p1"
    assert first["heading_path"] == "Methods/Results"
    assert "score" in first and 0 < first["score"] <= 1


def test_socket_query_requires_embedding(worker_env):
    _, _, sock = worker_env
    resp = _rpc(sock, {
        "schema_version": 1, "request_id": "req-2",
        "command": "query", "params": {"query": "no embedding here"},
    })
    assert resp["ok"] is False
    assert resp["error"]["code"] == "handler_error"


def test_socket_score_idempotent_replay(worker_env):
    """Score twice with same business inputs → one row only (idempotent)."""
    _, _, sock = worker_env
    payload = {
        "schema_version": 1, "request_id": "req-3", "command": "score",
        "params": {
            "pmid": "p1", "project": "npca", "formula_version": "v3",
            "rubric_version": "r1", "quality_final": 0.85, "quality_status": "scored",
            "source": "curator", "source_event_id": "evt-1",
            "criterion_validity": 0.9, "outcome_reliability": 0.8,
            "conclusion_data_consistency": 0.85,
        },
    }
    r1 = _rpc(sock, payload)
    assert r1["ok"] is True and r1["result"]["inserted"] is True

    payload["request_id"] = "req-4"            # same business inputs, new request id
    r2 = _rpc(sock, payload)
    assert r2["ok"] is True and r2["result"]["inserted"] is False


def test_socket_health(worker_env):
    _, _, sock = worker_env
    resp = _rpc(sock, {"schema_version": 1, "request_id": "req-5", "command": "health"})
    assert resp["ok"] is True


def test_socket_invalid_request_rejected(worker_env):
    _, _, sock = worker_env
    # missing schema_version — encode_frame rejects it, so send the raw JSON line
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(sock))
    try:
        s.sendall(b'{"request_id": "req-6", "command": "health"}\n')
        reader = FrameReader()
        reply = None
        while True:
            data = s.recv(65536)
            if not data:
                break
            frames = reader.feed(data)
            if frames:
                reply = frames[0]
                break
    finally:
        s.close()
    assert reply is not None
    assert reply["ok"] is False
    assert reply["error"]["code"] == "invalid_request"


def test_socket_unknown_command(worker_env):
    _, _, sock = worker_env
    resp = _rpc(sock, {"schema_version": 1, "request_id": "req-7", "command": "frobnicate"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "handler_error"