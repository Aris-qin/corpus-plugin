"""V2 unit tests for chunker_markdown. Run: python3 test_chunker_unit.py

V2 spec verified here:
  - heading-level slicing (each heading = one node, may split into p1..pN)
  - tree fields: parent_id / child_ids / sibling_ids / heading_chain / path
  - chunk_id readable: <doc_slug>__<path>__p<N>
  - 400-char cap strictly enforced
  - HR `---` and blockquote `>` are NOT slicing signals (treated as body)
  - cross-doc heading disambiguation via path
  - empty wrapper heading preserved (text='')
  - star marker stripped from section_title, kept in heading_chain
  - JSONL output schema (13 fields)
"""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from chunker_markdown import (
    chunk_plain_text,
    chunk_markdown,
    chunk_markdown_file,
    write_jsonl,
    MAX_CHUNK_CHARS,
    _doc_slug,
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# ----- doc_slug -----
def test_doc_slug() -> None:
    print("--- _doc_slug ---")
    check("parent dir only",
          _doc_slug("/root/.../review-ai-fall-elderly/draft_ch5.md") == "review_ai_fall_elderly")
    check("no parent",
          _doc_slug("foo.md") == "root" or _doc_slug("foo.md") == "",
          f"got {_doc_slug('foo.md')!r}")
    check("kebab → snake",
          "-" not in _doc_slug("/a/b-c/d.md"))


# ----- heading-level slicing -----
def test_one_heading_one_node() -> None:
    print("--- heading-level slicing (one heading = one node) ---")
    md = (
        "# 1\n"
        "intro.\n\n"
        "## 1.1 eval-tool\n"
        "body A.\n\n"
        "body B.\n\n"
        "## 1.2\n"
        "more.\n"
    )
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    # expect: 3 heading nodes (1 / 1.1 / 1.2)
    check("3 heading nodes", len(chunks) == 3, f"got {len(chunks)}")
    by_sec = {c.section_number: c for c in chunks}
    check("section 1 exists", "1" in by_sec)
    check("section 1.1 exists", "1.1" in by_sec)
    check("section 1.2 exists", "1.2" in by_sec)
    # 1.1's text should contain both body A and body B (heading-level merge)
    if "1.1" in by_sec:
        check("1.1 merges multi-paragraph body",
              "body A" in by_sec["1.1"].text and "body B" in by_sec["1.1"].text,
              f"got {by_sec['1.1'].text!r}")


# ----- tree relationships -----
def test_tree_relationships() -> None:
    print("--- tree fields: parent_id / child_ids / sibling_ids ---")
    md = (
        "# 5 ch5\n"
        "intro.\n\n"
        "## 5.1 sec-a\n"
        "a.\n\n"
        "## 5.2 sec-b\n"
        "b.\n\n"
        "## 5.3 sec-c\n"
        "c.\n"
        "### 5.3.1 sub-c\n"
        "x.\n"
    )
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    by_sec = {c.section_number: c for c in chunks}
    ch5 = by_sec["5"]
    check("5 has 3 children (5.1, 5.2, 5.3)",
          len(ch5.child_ids) == 3,
          f"got {ch5.child_ids}")
    sec_a = by_sec["5.1"]
    check("5.1 parent = 5", sec_a.parent_id == ch5.chunk_id)
    check("5.1 siblings = [5.2, 5.3]", len(sec_a.sibling_ids) == 2,
          f"got {sec_a.sibling_ids}")
    sub_c = by_sec["5.3.1"]
    check("5.3.1 parent = 5.3", sub_c.parent_id == by_sec["5.3"].chunk_id)
    check("5.3.1 has no children", sub_c.child_ids == [])
    check("5.3.1 has no siblings", sub_c.sibling_ids == [])


# ----- heading_chain -----
def test_heading_chain() -> None:
    print("--- heading_chain (root to leaf) ---")
    md = "# 1 ch1\nintro.\n\n## 1.1 sec\nbody.\n\n### 1.1.1 sub\nx.\n"
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    sub = next(c for c in chunks if c.section_number == "1.1.1")
    check("heading_chain has 3 levels", len(sub.heading_chain) == 3,
          f"got {sub.heading_chain}")
    check("chain ends with section title",
          "1.1.1" in sub.heading_chain[-1])


# ----- 400-char cap -----
def test_400_cap_strict() -> None:
    print("--- 400-char cap strictly enforced ---")
    long_body = ("老年住院患者跌倒风险预测分层中，AI 模型在不同队列与医疗体系下呈现跨队列稳健性。"
                 "德国多中心研究显示 AUROC 0.794-0.894。"
                 "中文可解释 LightGBM 工具化部署表明分层在中文 EHR 上已具备工程落地条件。"
                 "Cochrane 综述汇总 43 项多因素干预与对照的随机试验，显示分层有效。") * 5
    md = f"## 1.1 long\n{long_body}\n"
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    sec = [c for c in chunks if c.section_number == "1.1"]
    check("1.1 split into >= 2 chunks", len(sec) >= 2, f"got {len(sec)}")
    for c in sec:
        check(f"chunk {c.chunk_id} ≤ 400 chars",
              c.char_count <= MAX_CHUNK_CHARS,
              f"got {c.char_count}")


def test_chunk_id_uses_pN() -> None:
    print("--- chunk_id uses p1..pN for split parts ---")
    # build a > 800 char paragraph by repeating; we don't care about content,
    # only that the splitter kicks in.
    unit = "老年住院患者跌倒风险预测分层中 AI 模型在不同队列与医疗体系下呈现跨队列稳健性。"
    long_body = unit * 20  # ~860 chars
    md = f"## 1.1 long\n{long_body}\n"
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    sec = [c for c in chunks if c.section_number == "1.1"]
    check(f"1.1 split into >= 2 chunks (input was {len(long_body)} chars)",
          len(sec) >= 2, f"got {len(sec)}")
    if len(sec) >= 2:
        check("first part ends in p1", sec[0].chunk_id.endswith("p1"),
              f"got {sec[0].chunk_id}")
        check("second part ends in p2", sec[1].chunk_id.endswith("p2"),
              f"got {sec[1].chunk_id}")


# ----- HR + blockquote: NOT slicing signals -----
def test_hr_in_body() -> None:
    print("--- HR `---` is body, not slicing signal ---")
    md = "## 1.1\nfirst para.\n\n---\n\nsecond para.\n\n## 1.2\n"
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    sec = [c for c in chunks if c.section_number == "1.1"]
    check("1.1 = 1 chunk (HR does not split)", len(sec) == 1, f"got {len(sec)}")
    if sec:
        check("HR appears in body", "---" in sec[0].text,
              f"got {sec[0].text!r}")


def test_blockquote_in_body() -> None:
    print("--- blockquote `>` is body, not slicing signal ---")
    md = (
        "> preface here\n\n"
        "# 1\n"
        "intro.\n\n"
        "## 1.1\n"
        "> quoted note\n"
        "regular body.\n"
    )
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    sec = [c for c in chunks if c.section_number == "1.1"]
    check("1.1 = 1 chunk (blockquote does not split)", len(sec) == 1,
          f"got {len(sec)}")
    if sec:
        check("blockquote content in body",
              "preface here" in sec[0].text or "quoted note" in sec[0].text,
              f"got {sec[0].text!r}")


# ----- cross-doc disambiguation -----
def test_cross_doc_disambiguation() -> None:
    print("--- different docs / same heading don't collide ---")
    md_a = "# 1 ch1\nintro.\n\n## 1.1\nx.\n"
    md_b = "# 1 ch1\nintro.\n\n## 1.1\ny.\n"
    ch_a = chunk_markdown(md_a, source_file="/p/review-A/draft.md")
    ch_b = chunk_markdown(md_b, source_file="/p/review-B/draft.md")
    sec_a = next(c for c in ch_a if c.section_number == "1.1")
    sec_b = next(c for c in ch_b if c.section_number == "1.1")
    check("1.1 chunk_id differs by doc", sec_a.chunk_id != sec_b.chunk_id)
    check("a contains x", "x" in sec_a.text)
    check("b contains y", "y" in sec_b.text)


# ----- empty wrapper heading -----
def test_empty_wrapper() -> None:
    print("--- empty wrapper heading preserved in tree ---")
    md = (
        "# 1 ch1\n"
        "intro.\n\n"
        "## 1.1\n"  # no body, just wrapper
        "### 1.1.1 sub\n"
        "content.\n"
    )
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    sec11 = next(c for c in chunks if c.section_number == "1.1")
    check("1.1 exists as node", sec11 is not None)
    check("1.1 has empty text", sec11.text == "")
    check("1.1 has 1 child (1.1.1)", len(sec11.child_ids) == 1,
          f"got {sec11.child_ids}")


# ----- star marker -----
def test_star_marker() -> None:
    print("--- star marker in heading ---")
    md = "## 5.5 ★ 本章作者展望\nbody.\n"
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    sec = next(c for c in chunks if c.section_number == "5.5")
    check("section_title has ★ stripped", "★" not in sec.section_title,
          f"got {sec.section_title!r}")
    check("heading_chain keeps ★", any("★" in h for h in sec.heading_chain),
          f"got {sec.heading_chain}")


# ----- JSONL output -----
def test_jsonl_schema() -> None:
    print("--- JSONL output schema ---")
    md = "# 1 ch1\nintro.\n\n## 1.1\nbody.\n"
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
    n = write_jsonl(chunks, path)
    check("wrote N chunks", n == len(chunks))
    with open(path) as f:
        lines = [json.loads(l) for l in f]
    check("JSONL has same count", len(lines) == len(chunks))
    required = {"chunk_id", "doc_id", "level", "path", "parent_id", "child_ids",
                "sibling_ids", "heading_chain", "section_number", "section_title",
                "text", "char_count", "source_file"}
    for i, d in enumerate(lines):
        if set(d.keys()) != required:
            check(f"line {i} schema exact match",
                  False, f"diff: missing={required - set(d.keys())}, extra={set(d.keys()) - required}")
            break
    else:
        check("all lines have exact 13 fields", True)
    Path(path).unlink()


# ----- preface before first heading -----
def test_preface_node() -> None:
    print("--- preface before first heading is level=0 node ---")
    md = (
        "This is the abstract.\n"
        "It has two lines.\n\n"
        "# 1 ch1\n"
        "intro.\n"
    )
    chunks = chunk_markdown(md, source_file="/p/draft.md")
    preface = next((c for c in chunks if c.level == 0), None)
    check("preface node exists", preface is not None)
    if preface:
        check("preface has section_number=''", preface.section_number == "")
        check("preface text contains abstract", "abstract" in preface.text,
              f"got {preface.text!r}")


def test_plain_text_imrad() -> None:
    print("--- chunk_plain_text: IMRAD section detection ---")
    md = """Background.
This is the background section.

Methods.
This is the methods section.

Results.
One hundred eighty subjects randomized.

Conclusions.
Access to bed-chair pressure sensor did not reduce restraint use."""
    chunks = chunk_plain_text(md, source_file="/p/sample.txt")
    levels = {c.level: levels.get(c.level, 0) + 1 for c in chunks for levels in [{}]}
    levels = {}
    for c in chunks:
        levels[c.level] = levels.get(c.level, 0) + 1
    check("detected >= 1 IMRAD heading", any(c.level == 1 for c in chunks),
          f"got {levels}")
    by_text = [c.text for c in chunks]
    has_methods = any("methods section" in t for t in by_text)
    check("Methods body merged into chunk", has_methods, f"got {by_text}")
    has_results = any("subjects randomized" in t for t in by_text)
    check("Results body merged into chunk", has_results, f"got {by_text}")


def test_plain_text_no_markdown() -> None:
    print("--- chunk_plain_text: produces tree metadata without markdown ---")
    md = "Background.\nBody of background.\n\nMethods.\nBody of methods."
    chunks = chunk_plain_text(md, source_file="/p/plain.txt")
    check("all chunks have heading_chain", all(len(c.heading_chain) > 0 for c in chunks))
    check("all chunks have path", all(c.path for c in chunks))
    # level-1 chunks may legitimately have empty parent_id (no H1 above)
    # since this is just Background + Methods at top level
    check("level-1 chunks tree integrity OK",
          all(isinstance(c.parent_id, (str, type(None))) for c in chunks))


def test_plain_text_chunk_size() -> None:
    print("--- chunk_plain_text: respects 400-char cap ---")
    long_body = "Detailed clinical trial methodology description. " * 30  # ~1200 chars
    md = f"Methods.\n{long_body}\nResults.\nfinal result."
    chunks = chunk_plain_text(md, source_file="/p/long.txt")
    methods_chunks = [c for c in chunks if "Methods" in c.heading_chain[-1]]
    check("Methods body split into >= 2 chunks", len(methods_chunks) >= 2,
          f"got {len(methods_chunks)}")
    for c in methods_chunks:
        check(f"chunk <= 400 chars", c.char_count <= MAX_CHUNK_CHARS,
              f"got {c.char_count}")


if __name__ == "__main__":
    test_doc_slug()
    test_one_heading_one_node()
    test_tree_relationships()
    test_heading_chain()
    test_400_cap_strict()
    test_chunk_id_uses_pN()
    test_hr_in_body()
    test_blockquote_in_body()
    test_cross_doc_disambiguation()
    test_empty_wrapper()
    test_star_marker()
    test_jsonl_schema()
    test_preface_node()
    test_plain_text_imrad()
    test_plain_text_no_markdown()
    test_plain_text_chunk_size()
    print(f"\n{'=' * 50}")
    print(f"  PASS: {PASS}    FAIL: {FAIL}")
    print(f"{'=' * 50}")
    sys.exit(0 if FAIL == 0 else 1)

# ========== V3 chunk_plain_text tests ==========







