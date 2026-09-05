"""Verify chunker across multiple markdown files.

Usage:
    python3 test_chunker.py                      # all 6 ch files
    python3 test_chunker.py draft_ch5.md         # specific file in review-ai dir
    python3 test_chunker.py /abs/path/file.md    # absolute
"""
import json
import sys
from pathlib import Path
from collections import OrderedDict, Counter

sys.path.insert(0, str(Path(__file__).parent))
from chunker_markdown import chunk_markdown_file

REVIEW_DIR = Path("/root/.openclaw/workspace/projects/review-ai-fall-elderly")


def test_file(p: Path) -> dict:
    chunks = chunk_markdown_file(str(p))
    by_sec = OrderedDict()
    for c in chunks:
        by_sec.setdefault(c.section_number, []).append(c)
    lens = [c.char_count for c in chunks]
    levels = Counter(c.level for c in chunks)
    return {
        "file": p.name,
        "total_chunks": len(chunks),
        "unique_sections": len(by_sec),
        "min_chars": min(lens) if lens else 0,
        "max_chars": max(lens) if lens else 0,
        "median_chars": sorted(lens)[len(lens) // 2] if lens else 0,
        "levels": dict(levels),
        "by_section": {k: len(v) for k, v in by_sec.items()},
    }


def print_section(name: str, info: dict) -> None:
    print(f"\n{'=' * 60}")
    print(f"📄 {name}")
    print(f"{'=' * 60}")
    print(f"  chunks:        {info['total_chunks']}")
    print(f"  unique sections: {info['unique_sections']}")
    print(f"  char counts:   min={info['min_chars']} max={info['max_chars']} median={info['median_chars']}")
    print(f"  levels:        {info['levels']}")
    print(f"  sections:")
    for sec, cnt in info["by_section"].items():
        print(f"    {sec:8s} ({cnt} chunks)")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        targets = [Path(sys.argv[1]) if Path(sys.argv[1]).is_absolute() else REVIEW_DIR / sys.argv[1]]
    else:
        targets = sorted(REVIEW_DIR.glob("draft_ch*.md"))

    grand_total = 0
    for p in targets:
        if not p.exists():
            print(f"ERROR: {p} not found")
            continue
        info = test_file(p)
        print_section(p.name, info)
        grand_total += info["total_chunks"]
    print(f"\n{'=' * 60}")
    print(f"📊 GRAND TOTAL: {grand_total} chunks across {len(targets)} files")
    print(f"{'=' * 60}")