#!/usr/bin/env python3
"""
Re-embed chunks for a specific project using real qwen text-embedding-v4 API.

Usage:
    python3 reembed_project.py --project review-ai-fall-elderly [--batch-size 10] [--dry-run]
"""

import argparse
import json
import os
import sqlite3
import struct
import sys
import time
import urllib.request
from pathlib import Path

UNIFIED_CORPUS_DB = Path("/root/.openclaw/workspace/projects/_corpus/corpus.db")
OPENCLAW_CONFIG = Path(os.path.expanduser("~/.openclaw/openclaw.json"))


def get_qwen_config():
    """从 openclaw.json 读 qwen provider 配置"""
    with open(OPENCLAW_CONFIG) as f:
        cfg = json.load(f)
    qwen = cfg["models"]["providers"]["qwen"]
    return qwen["apiKey"], qwen["baseUrl"]


def embed_batch(texts: list[str], api_key: str, base_url: str, dim: int = 1024) -> list[list[float]]:
    """批量调用 qwen v4 embedding API (每次最多 10 行)"""
    url = base_url + "/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = json.dumps({
        "model": "text-embedding-v4",
        "input": texts,
        "dimensions": dim,
        "encoding_format": "float",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers=headers)
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    embeddings = [d["embedding"] for d in sorted(resp["data"], key=lambda x: x["index"])]
    return embeddings


def serialize_float32(vec: list[float]) -> bytes:
    """float list -> float32 little-endian bytes"""
    return struct.pack(f'<{len(vec)}f', *vec)


def main():
    parser = argparse.ArgumentParser(description="Re-embed project chunks with real qwen v4 API")
    parser.add_argument("--project", required=True, help="project slug")
    parser.add_argument("--batch-size", type=int, default=10, help="batch size (max 10 for v4)")
    parser.add_argument("--dry-run", action="store_true", help="只统计不执行")
    args = parser.parse_args()

    api_key, base_url = get_qwen_config()
    print(f"[reembed] project: {args.project}")
    print(f"[reembed] API: {base_url}/embeddings (text-embedding-v4, dim=1024)")
    print(f"[reembed] batch_size: {args.batch_size}")

    # 加载 vec0 扩展
    db = sqlite3.connect(str(UNIFIED_CORPUS_DB))
    db.enable_load_extension(True)
    db.load_extension("/usr/local/lib/node_modules/openclaw/node_modules/sqlite-vec-linux-x64/vec0.so")

    # 取该项目的所有 chunks
    rows = db.execute("""
        SELECT c.chunk_id, c.text
        FROM chunks c
        JOIN document_groups dg ON c.pmid = dg.pmid
        WHERE dg.project_slug = ?
        ORDER BY c.chunk_id
    """, (args.project,)).fetchall()
    
    print(f"[reembed] chunks to re-embed: {len(rows)}")
    
    if args.dry_run:
        print("[reembed] DRY RUN - skipping execution")
        db.close()
        return

    total = len(rows)
    batch_size = min(args.batch_size, 10)  # v4 max 10 per call
    done = 0
    failed = 0
    start_time = time.time()

    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        chunk_ids = [r[0] for r in batch]
        texts = [r[1][:8000] for r in batch]  # v4 max 8192 tokens per input

        try:
            embeddings = embed_batch(texts, api_key, base_url, dim=1024)
            
            # 写入 chunk_vectors
            for chunk_id, emb in zip(chunk_ids, embeddings):
                emb_blob = serialize_float32(emb)
                db.execute(
                    "UPDATE chunk_vectors SET embedding = ? WHERE chunk_id = ?",
                    (emb_blob, chunk_id)
                )
            db.commit()
            done += len(batch)
        except Exception as e:
            print(f"[reembed] ERROR batch {i}-{i+len(batch)}: {e}")
            failed += len(batch)
            db.rollback()
            # 失败后降级为单个重试
            for chunk_id, text in batch:
                try:
                    embeddings = embed_batch([text], api_key, base_url, dim=1024)
                    emb_blob = serialize_float32(embeddings[0])
                    db.execute(
                        "UPDATE chunk_vectors SET embedding = ? WHERE chunk_id = ?",
                        (emb_blob, chunk_id)
                    )
                    db.commit()
                    done += 1
                except Exception as e2:
                    print(f"[reembed]   retry failed for {chunk_id}: {e2}")
                    failed += 1
                    db.rollback()

        # 进度报告
        elapsed = time.time() - start_time
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        if (i // batch_size) % 10 == 0 or done >= total:
            print(f"[reembed] {done}/{total} ({done*100//total}%) | elapsed {elapsed:.0f}s | eta {eta:.0f}s | failed {failed}")

    # 更新 chunk_vectors_info
    db.execute(
        "INSERT OR REPLACE INTO chunk_vectors_info (key, value) VALUES (?, ?)",
        ("embed_model", "text-embedding-v4")
    )
    db.execute(
        "INSERT OR REPLACE INTO chunk_vectors_info (key, value) VALUES (?, ?)",
        ("embed_dim", 1024)
    )
    db.execute(
        "INSERT OR REPLACE INTO chunk_vectors_info (key, value) VALUES (?, ?)",
        ("rebuilt_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    )
    db.commit()

    elapsed = time.time() - start_time
    print(f"\n[reembed] ✅ DONE: {done}/{total} re-embedded, {failed} failed, {elapsed:.0f}s")
    print(f"[reembed] chunk_vectors_info updated: embed_model=text-embedding-v4, dim=1024")
    
    db.close()


if __name__ == "__main__":
    main()
