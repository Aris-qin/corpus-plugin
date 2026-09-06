#!/usr/bin/env python3
"""corpus-query-tool helper — chunk tree lookup + similar-level retrieval.

Used by the OpenClaw plugin (corpus-query-tool) for the two tools that need
direct sqlite access:
  - corpus_get_chunk         -> get a chunk's full tree context
  - corpus_list_similar_level -> KNN over chunks at the same heading level

Reuses the same sqlite-vec stack as scripts/corpus/db.py. Output is JSON on
stdout; errors are JSON {"error": ...} on stdout with exit code 1.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import sys
from pathlib import Path

# 自举: 本文件位于 plugins/corpus-query/,corpus/ 在 repo 根的上一级目录。
# 裸脚本运行(插件 spawn 桥接)时无包上下文,必须显式加 path。
_here = Path(__file__).resolve().parent
for _maybe in (_here.parent.parent, _here.parent.parent.parent):
    if (_maybe / "corpus" / "db.py").exists() and str(_maybe) not in sys.path:
        sys.path.insert(0, str(_maybe))

from corpus.config import config
CORPUS_DB = Path(config["paths"]["corpus_db"])
VEC_EXT = config["paths"].get("vec_ext", "")


def serialize_float32(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CORPUS_DB))
    # vec0 via python binding (native load_extension of raw vec0.so is ABI-incompatible
    # with the stdlib sqlite3 module: 'undefined symbol: sqlite3__init').
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def get_chunk_meta(conn: sqlite3.Connection, chunk_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT chunk_id, pmid, level, path, parent_id, child_ids, sibling_ids,
               heading_chain, heading_path, text, snippet, snippet_source,
               section_importance, doc_id
        FROM chunks WHERE chunk_id = ?
        """,
        (chunk_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "chunk_id": row[0],
        "pmid": row[1],
        "level": row[2],
        "path": row[3],
        "parent_id": row[4],
        "child_ids": json.loads(row[5]) if row[5] else [],
        "sibling_ids": json.loads(row[6]) if row[6] else [],
        "heading_chain": json.loads(row[7]) if row[7] else [],
        "heading_path": row[8],
        "text": row[9],
        "snippet": row[10],
        "snippet_source": row[11],
        "section_importance": row[12],
        "doc_id": row[13],
    }


def cmd_get_chunk(args: list[str]) -> None:
    if len(args) < 2 or args[0] != "--chunk-id":
        raise ValueError("get: --chunk-id <id> required")
    chunk_id = args[1]
    include = ["text", "metadata"]
    if "--include" in args:
        i = args.index("--include")
        include = [s.strip() for s in args[i + 1].split(",") if s.strip()]

    conn = open_db()
    try:
        meta = get_chunk_meta(conn, chunk_id)
        if not meta:
            raise ValueError(f"chunk_not_found: {chunk_id}")

        out: dict = {"chunk_id": chunk_id, "pmid": meta["pmid"]}

        if "text" in include:
            out["text"] = meta["text"]
        if "snippet" in include:
            out["snippet"] = meta["snippet"]
            out["snippet_source"] = meta["snippet_source"]
        if "metadata" in include:
            out["metadata"] = {
                "level": meta["level"],
                "path": meta["path"],
                "heading_path": meta["heading_path"],
                "heading_chain": meta["heading_chain"],
                "section_importance": meta["section_importance"],
                "doc_id": meta["doc_id"],
            }
        if "parent" in include:
            p = get_chunk_meta(conn, meta["parent_id"]) if meta["parent_id"] else None
            out["parent"] = {k: v for k, v in p.items() if k in ("chunk_id", "level", "path", "heading_path", "heading_chain")} if p else None
        if "children" in include:
            out["children"] = []
            for cid in meta["child_ids"]:
                c = get_chunk_meta(conn, cid)
                if c:
                    out["children"].append({
                        "chunk_id": c["chunk_id"], "level": c["level"], "path": c["path"],
                        "heading_path": c["heading_path"], "heading_chain": c["heading_chain"],
                    })
        if "siblings" in include:
            out["siblings"] = []
            for sid in meta["sibling_ids"]:
                s = get_chunk_meta(conn, sid)
                if s:
                    out["siblings"].append({
                        "chunk_id": s["chunk_id"], "level": s["level"], "path": s["path"],
                        "heading_path": s["heading_path"], "heading_chain": s["heading_chain"],
                    })

        print(json.dumps(out, ensure_ascii=False))
    finally:
        conn.close()


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def cmd_similar_level(args: list[str]) -> None:
    if len(args) < 2 or args[0] != "--chunk-id":
        raise ValueError("similar: --chunk-id <id> required")
    chunk_id = args[1]
    top_k = 5
    cross_document = True
    if "--top-k" in args:
        top_k = max(1, min(int(args[args.index("--top-k") + 1]), 20))
    if "--same-doc" in args:
        cross_document = False

    conn = open_db()
    try:
        ref = conn.execute(
            "SELECT embedding FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if not ref:
            raise ValueError(f"chunk_not_found_no_vector: {chunk_id}")
        ref_meta = get_chunk_meta(conn, chunk_id)
        if not ref_meta:
            raise ValueError(f"chunk_not_found: {chunk_id}")

        ref_level = ref_meta["level"]
        ref_path = ref_meta["path"] or ""
        ref_depth = len([s for s in ref_path.split("/") if s])
        ref_chain = ref_meta["heading_chain"]
        ref_doc = ref_meta["doc_id"]

        # KNN over-fetch (4x) then post-filter by level + depth
        # chunk_vectors.embedding is a raw little-endian float32 blob (vec0)
        blob = ref[0] if isinstance(ref[0], (bytes, bytearray)) else serialize_float32(json.loads(ref[0]))
        knn = conn.execute(
            "SELECT chunk_id, distance FROM chunk_vectors WHERE embedding MATCH ? AND k = ?",
            (blob, max(top_k * 4, 40)),
        ).fetchall()

        results = []
        for cid, dist in knn:
            if cid == chunk_id:
                continue
            m = get_chunk_meta(conn, cid)
            if not m:
                continue
            if m["level"] != ref_level:
                continue
            if not cross_document and m["doc_id"] != ref_doc:
                continue
            depth = len([s for s in (m["path"] or "").split("/") if s])
            if abs(depth - ref_depth) > 1:
                continue
            sim = 1.0 - min(dist, 2.0) / 2.0  # distance -> similarity approx
            jac = jaccard(ref_chain, m["heading_chain"])
            score = 0.6 * sim + 0.4 * jac
            results.append({
                "chunk_id": cid,
                "pmid": m["pmid"],
                "level": m["level"],
                "path": m["path"],
                "heading_path": m["heading_path"],
                "heading_chain": m["heading_chain"],
                "doc_id": m["doc_id"],
                "vec_similarity": round(sim, 4),
                "heading_jaccard": round(jac, 4),
                "score": round(score, 4),
                "text_preview": (m["text"] or "")[:200],
            })

        results.sort(key=lambda r: -r["score"])
        print(json.dumps({
            "reference": {
                "chunk_id": chunk_id,
                "level": ref_level,
                "path": ref_path,
                "heading_chain": ref_chain,
            },
            "results": results[:top_k],
        }, ensure_ascii=False))
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) < 2:
        raise ValueError("usage: chunk_query.py get|similar ...")
    cmd = sys.argv[1]
    args = sys.argv[2:]
    try:
        if cmd == "get":
            cmd_get_chunk(args)
        elif cmd == "similar":
            cmd_similar_level(args)
        else:
            raise ValueError(f"unknown command: {cmd}")
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
