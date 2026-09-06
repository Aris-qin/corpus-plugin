"""Contract tests for corpus.canonical (IMPLEMENTATION.md §1).

Covers: node_id stability (duplicate titles / front insertion / re-run
determinism), per-kind text rules, tree invariants, illegal rejection,
golden fixture equality, and process_raw's adapter dispatch (no legacy chunker).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import canonical as C

FIX = Path(__file__).parent / "fixtures" / "canonical"
H = "sha256:" + "a" * 64


# --------------------------------------------------------------------------- #
# node_id stability
# --------------------------------------------------------------------------- #

def _ids_by_text(doc: dict) -> dict[str, str]:
    return {n["text"]: n["node_id"] for n in doc["nodes"]}


def test_node_id_deterministic_rerun():
    md = "## Methods\n\nbody text here\n"
    a = C.adapt_markdown(md, source_file="x.md", content_hash=H)
    b = C.adapt_markdown(md, source_file="x.md", content_hash=H)
    assert [n["node_id"] for n in a["nodes"]] == [n["node_id"] for n in b["nodes"]]


def test_node_id_duplicate_titles_distinct():
    md = "## Results\n\nalpha\n\n## Results\n\nbeta\n"
    doc = C.adapt_markdown(md, source_file="x.md", content_hash=H)
    heading_ids = [n["node_id"] for n in doc["nodes"] if n["text"] == "Results"]
    assert len(heading_ids) == 2
    assert heading_ids[0] != heading_ids[1]


def test_node_id_stable_under_front_insertion():
    # With content_hash held fixed, inserting an unrelated heading before "Methods"
    # must not shift the ids of Methods or its body (occurrence-based canonical_path).
    base = "## Methods\n\nbody text here\n"
    inserted = "## Intro\n\nintro body\n\n## Methods\n\nbody text here\n"
    b = _ids_by_text(C.adapt_markdown(base, source_file="x.md", content_hash=H))
    i = _ids_by_text(C.adapt_markdown(inserted, source_file="x.md", content_hash=H))
    assert b["Methods"] == i["Methods"]
    assert b["body text here"] == i["body text here"]


def test_node_id_ordinal_locator_do_not_participate():
    # Two nodes with identical canonical_path/kind/text but different ordinal must
    # hash identically (ordinal/locator excluded from the payload).
    id1, full1 = C.compute_node_id(H, "/heading:0", "heading", "Methods")
    id2, full2 = C.compute_node_id(H, "/heading:0", "heading", "Methods")
    assert id1 == id2 and full1 == full2
    assert id1.startswith("nd_") and len(id1) == 3 + 12


def test_node_id_collision_escalation():
    # Force a 12-hex prefix collision between two *distinct* nodes; escalation to
    # 16 hex must separate them and set node_id_full.
    nodes = [
        C.Node(kind="paragraph", text="alpha"),
        C.Node(kind="paragraph", text="beta"),
    ]
    C._finalize_tree(nodes)

    def fake_hash(payload: str) -> str:
        # identical first 12 hex, diverge afterwards → escalation resolves it
        if "alpha" in payload:
            return "abcdef012345" + "0" * 52
        if "beta" in payload:
            return "abcdef012345" + "f" * 52
        return "0" * 64

    C.assign_node_ids(nodes, H, hash_fn=fake_hash)
    assert nodes[0].node_id != nodes[1].node_id
    assert nodes[0].node_id_full and nodes[1].node_id_full


def test_node_id_unresolvable_collision_rejected():
    nodes = [C.Node(kind="paragraph", text="a"), C.Node(kind="paragraph", text="b")]
    C._finalize_tree(nodes)

    def fake_hash(payload: str) -> str:
        # identical through 20 hex, distinct fulls → cannot be separated → reject.
        # Distinguish the two nodes by their trailing text token (the payload ends
        # with "\0<text_normalized>"); both share canonical_path "/paragraph:0"
        # because the occurrence counter is keyed by (kind, text), so different
        # text → each is occurrence 0. Keying on canonical_path would collapse them.
        if payload.endswith("\x00a"):
            return "0" * 20 + "a" * 44
        return "0" * 20 + "b" * 44

    with pytest.raises(C.NodeIdCollision):
        C.assign_node_ids(nodes, H, hash_fn=fake_hash)


# --------------------------------------------------------------------------- #
# per-kind text rules
# --------------------------------------------------------------------------- #

def test_each_kind_text_rule():
    md = (
        "# Head One\n\n"
        "A normalized   paragraph  with   spaces.\n\n"
        "- one\n- two\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "$$\\frac{a}{b}$$\n\n"
        "Figure 1. A caption line.\n\n"
        "![alt text](u.png)\n\n"
        "[^x]: footnote body.\n\n"
        "$z_i$\n"
    )
    doc = C.adapt_markdown(md, source_file="x.md", content_hash=H)
    by_kind = {}
    for n in doc["nodes"]:
        by_kind.setdefault(n["kind"], []).append(n)

    assert by_kind["heading"][0]["text"] == "Head One"        # markup-stripped title
    assert by_kind["paragraph"][0]["text"] == "A normalized paragraph with spaces."  # collapsed ws
    assert by_kind["list"][0]["text"] == "- one\n- two"        # one item per line, '- ' prefix
    assert "|" in by_kind["table"][0]["text"]                  # GFM markdown
    assert by_kind["table"][0]["attrs"]["markdown"]
    assert by_kind["formula"][0]["text"] == "\\frac{a}{b}"     # canonical latex, no $$
    assert by_kind["formula"][0]["attrs"]["display"] is True
    assert by_kind["inline_formula"][0]["attrs"]["display"] is False
    assert by_kind["caption"][0]["text"].startswith("Figure 1")
    assert by_kind["figure"][0]["text"] == "alt text"
    assert by_kind["figure"][0]["attrs"]["uri"] == "u.png"
    assert by_kind["footnote"][0]["text"] == "footnote body."


def test_figure_without_alt_emits_warning_and_placeholder():
    doc = C.adapt_markdown("![](f.png)\n", source_file="x.md", content_hash=H)
    fig = [n for n in doc["nodes"] if n["kind"] == "figure"][0]
    assert fig["text"] == "[figure]"
    assert any(w["level"] == "warn" and w["code"] == "figure_no_alt"
               for w in doc["parser_warnings"])


def test_heading_level_and_nonheading_title_rules():
    with pytest.raises(C.InvalidNodeError):
        C.validate_node(C.Node(kind="heading", level=7, title="X", text="X"))
    with pytest.raises(C.InvalidNodeError):
        C.validate_node(C.Node(kind="paragraph", level=2, text="x"))
    with pytest.raises(C.InvalidNodeError):
        C.validate_node(C.Node(kind="paragraph", title="oops", text="x"))


def test_table_and_formula_attrs_required():
    with pytest.raises(C.InvalidNodeError):
        C.validate_node(C.Node(kind="table", text="| a |", attrs={}))
    with pytest.raises(C.InvalidNodeError):
        C.validate_node(C.Node(kind="formula", text="x", attrs={"latex": "x"}))  # missing display


# --------------------------------------------------------------------------- #
# tree invariants
# --------------------------------------------------------------------------- #

def test_tree_invariants_hold_on_adapter_output():
    doc = C.document_from_dict(
        C.adapt_markdown((FIX / "md" / "full_features.input").read_text(), source_file="f.md",
                         content_hash=H)
    )
    C.validate_document(doc)  # must not raise
    ordinals = [n.ordinal for n in doc.nodes]
    assert ordinals == sorted(ordinals) and len(set(ordinals)) == len(ordinals)
    by_id = {n.node_id: n for n in doc.nodes}
    for n in doc.nodes:
        if n.parent_id:
            assert n.parent_id in by_id
            assert n.path.startswith(by_id[n.parent_id].path + "/")


def test_heading_gap_compression():
    # H1 then H3 (skips H2) → effective levels 1,2 (no skip).
    doc = C.adapt_markdown("# Top\n\n### Skipped\n\nbody\n", source_file="x.md", content_hash=H)
    heads = [n for n in doc["nodes"] if n["kind"] == "heading"]
    assert [h["level"] for h in heads] == [1, 2]


def test_error_warning_rejected():
    doc = C.document_from_dict(C.adapt_markdown("para\n", source_file="x.md", content_hash=H))
    doc.parser_warnings.append({"level": "error", "code": "boom", "msg": "bad parse"})
    with pytest.raises(C.TreeInvariantError):
        C.validate_document(doc)


def test_bad_ordinal_rejected():
    doc = C.document_from_dict(C.adapt_markdown("a\n\nb\n", source_file="x.md", content_hash=H))
    doc.nodes[1].ordinal = doc.nodes[0].ordinal  # break strict increase
    with pytest.raises(C.TreeInvariantError):
        C.validate_document(doc)


def test_dangling_parent_rejected():
    doc = C.document_from_dict(C.adapt_markdown("# H\n\nbody\n", source_file="x.md", content_hash=H))
    body = [n for n in doc.nodes if n.kind == "paragraph"][0]
    body.parent_id = "nd_deadbeef0000"
    with pytest.raises(C.TreeInvariantError):
        C.validate_document(doc)


def test_bad_content_hash_rejected():
    with pytest.raises(C.CanonicalError):
        C.build_document([C.Node(kind="paragraph", text="x")], doc_id="d",
                         content_hash="not-a-hash", source_file="x", parser="p",
                         parser_version="1.0")


# --------------------------------------------------------------------------- #
# golden fixtures
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("golden", sorted(FIX.glob("*/*.canon.json")))
def test_golden_fixtures_roundtrip(golden):
    expected = json.loads(golden.read_text())
    raw = golden.with_suffix("").with_suffix(".input").read_bytes()
    chash = C.content_hash_of(raw)
    sub = golden.parent.name
    adapter = C.adapt_markdown if sub == "md" else C.adapt_txt
    got = adapter(raw.decode("utf-8"), source_file=expected["source_file"], content_hash=chash)
    assert got == expected


# --------------------------------------------------------------------------- #
# process_raw dispatch
# --------------------------------------------------------------------------- #

def test_process_raw_prefers_canon(tmp_path):
    canon = tmp_path / "doc.canon.json"
    doc = C.adapt_markdown("# H\n\nbody\n", source_file="x.md", content_hash=H)
    canon.write_text(json.dumps(doc))
    res = C.process_raw("33875643", canon_path=canon)
    assert res.source == "canon"
    assert res.adapter is None
    assert res.used_legacy_chunker is False


def test_process_raw_falls_back_to_adapter_no_legacy_chunker(tmp_path):
    legacy = tmp_path / "33875643.md"
    legacy.write_text("# H\n\nbody text\n")
    res = C.process_raw("33875643", canon_path=tmp_path / "missing.canon.json", legacy_path=legacy)
    assert res.source == "adapter"
    assert res.adapter == "adapt_markdown"
    assert res.used_legacy_chunker is False
    assert res.document.parser == "legacy_md"


def test_process_raw_txt_adapter(tmp_path):
    legacy = tmp_path / "33875643.txt"
    legacy.write_text("Abstract\n\nsome body text here\n")
    res = C.process_raw("33875643", legacy_path=legacy)
    assert res.adapter == "adapt_txt"
    assert res.document.parser == "legacy_txt"


def test_canonicalize_file_atomic_write(tmp_path):
    src = tmp_path / "a.md"
    src.write_text("# Title\n\nbody\n")
    out = tmp_path / "out" / "a.canon.json"
    doc = C.canonicalize_file(src, output=out)
    assert out.exists()
    assert not out.with_suffix(out.suffix + ".tmp").exists()
    assert doc.parser == "legacy_md"


def test_canonicalize_rich_format_not_implemented():
    with pytest.raises(NotImplementedError):
        C.canonicalize(b"%PDF-1.4", source_file="x.pdf", parser_version="1.0")
