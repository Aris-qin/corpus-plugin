"""V2 hierarchical markdown chunker — heading-level slicing.

Design (V2):
  - **One heading = one logical chunk node** (multiple chunks if body > MAX_CHUNK_CHARS).
  - Every heading node carries tree metadata:
      chunk_id      : `<doc_slug>__<h1>__<h2>__<h3>__p<N>`  (readable, file-distinct)
      level         : 1 / 2 / 3
      path          : "/ch5/5.3/5.3.1"  (tree path; prefix-match friendly)
      parent_id     : parent heading's chunk_id (None for level=1)
      child_ids     : child heading chunk_ids
      sibling_ids   : siblings under same parent
      heading_chain : list of titles from root to this node
      section_number: "5" / "5.3" / "5.3.1"
      section_title : clean heading title (★ marker stripped)
      text          : paragraph body for this chunk
      char_count    : len(text)
      source_file   : file the chunk came from
  - **No slicing signal from HR `---` or blockquote `>`** — both treated as body text.
  - **MAX_CHUNK_CHARS = 400** (per L decision 2026-07-19):
      heading body > 400 → split by blank line > sentence-ending punctuation
  - **Sibling chunks** (same heading, > 400 chars, split into N parts):
      all share the same parent_id / child_ids / sibling_ids / heading_chain;
      their chunk_id suffix `p1`, `p2`, ... `pN` distinguishes them.
  - **Sibling headings** (e.g. 5.3.1, 5.3.2 under 5.3) — sibling_ids here = list of
      other *heading-chunk-ids* (not internal paragraph parts).
  - **Blockquote `>`** : stripped of leading `>` and kept as body line.
  - **Code fence**: preserved verbatim inside body (headings inside fences ignored).

JSONL output: one chunk per line, with stable field order. Suitable for SQLite
ingest + sqlite-vec; tree reconstruction via prefix-match on `path`.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


MAX_CHUNK_CHARS = 400  # per L decision 2026-07-19, body split threshold

HEADING_PATTERNS = {
    1: re.compile(r"^#\s+(?P<payload>.+?)\s*$"),
    2: re.compile(r"^##\s+(?P<payload>.+?)\s*$"),
    3: re.compile(r"^###\s+(?P<payload>.+?)\s*$"),
}
SECTION_NUM_RE = re.compile(r"^\d+(?:\.\d+)*$")
CODE_FENCE_RE = re.compile(r"^```")
BLOCKQUOTE_RE = re.compile(r"^>\s?")
HR_RE = re.compile(r"^-{3,}\s*$")
STAR_MARKER_RE = re.compile(r"^★\s*")
# sentence-ending punctuation for split fallback
SENT_END_PUNCTS = ["。", ".", "！", "!", "？", "?"]


@dataclass
class HeadingNode:
    """A heading + its body. May produce multiple chunks if body > MAX_CHUNK_CHARS."""
    level: int
    section_number: str       # "5" / "5.3" / "5.3.1"  ("" if none)
    section_title: str        # clean title (★ stripped)
    heading_path: str         # "Ch5 出院转介 > 5.3 本地模型"
    path: str                 # "/ch5/5.3"
    heading_chain: list[str]  # ["Ch5 出院转介", "5.3 本地模型"]
    parent_id: Optional[str]  # parent heading chunk_id (None if level=1)
    body_lines: list[str] = field(default_factory=list)

    # filled after construction:
    child_ids: list[str] = field(default_factory=list)
    sibling_ids: list[str] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)  # one or more (if split)


@dataclass
class MarkdownChunk:
    chunk_id: str
    doc_id: str
    level: int
    path: str
    parent_id: Optional[str]
    child_ids: list[str]
    sibling_ids: list[str]
    heading_chain: list[str]
    section_number: str
    section_title: str
    text: str
    char_count: int
    source_file: str


def _parse_section_num(payload: str) -> tuple[str, str]:
    """Split heading payload into (section_number, title).
      '5.1'         -> ('5.1', '')
      '5.5 ★ 节'    -> ('5.5', '★ 节')
      '评估工具'     -> ('', '评估工具')
    """
    parts = payload.split(None, 1)
    if not parts:
        return "", ""
    first, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    if SECTION_NUM_RE.match(first):
        return first, rest
    return "", payload


def _strip_star(title: str) -> str:
    """Strip leading ★ marker for section_title."""
    return STAR_MARKER_RE.sub("", title).strip()


def _doc_slug(source_file: str) -> str:
    """Derive doc_id from source_file's parent directory (project slug).

       /root/.../review-ai-fall-elderly/draft_ch5.md
       -> 'review_ai_fall_elderly'

    Per-file disambiguation comes from path (e.g. /ch5) inside chunk_id, so
    multiple files in same project (ch1..ch6) stay distinct without bloating doc_id.
    """
    p = Path(source_file)
    parent = p.parent.name or "root"
    s = parent.replace("-", "_").replace(".", "_")
    return s or "root"


def _split_oversize_body(lines: list[str], cap: int = MAX_CHUNK_CHARS) -> list[list[str]]:
    """Split a list of body lines into N groups, each ≤ cap chars (joined).

    Strategy:
      1. Group consecutive lines into paragraphs (split on blank lines).
      2. Accumulate paragraphs into chunks; if next paragraph would exceed cap,
         start a new chunk.
      3. If a single paragraph > cap, split by sentence-ending punctuation,
         falling back to hard cap (worst case).
    """
    # step 1: paragraphs
    paragraphs: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if not ln.strip():
            if cur:
                paragraphs.append(cur)
                cur = []
        else:
            cur.append(ln)
    if cur:
        paragraphs.append(cur)

    # step 2: pack into chunks ≤ cap
    chunks: list[list[str]] = []
    cur_chunk: list[str] = []
    cur_len = 0
    for para in paragraphs:
        para_text = "\n".join(para)
        para_len = len(para_text) + 1  # +1 for join \n
        if para_len > cap:
            # flush current chunk first
            if cur_chunk:
                chunks.append(cur_chunk)
                cur_chunk = []
                cur_len = 0
            # split this single paragraph by sentence endings
            chunks.extend(_split_by_sentence(para_text, cap))
            continue
        if cur_len + para_len > cap and cur_chunk:
            chunks.append(cur_chunk)
            cur_chunk = []
            cur_len = 0
        cur_chunk.append(para_text)
        cur_len += para_len
    if cur_chunk:
        chunks.append(cur_chunk)

    # step 3: strip leading/trailing blanks per chunk
    out: list[list[str]] = []
    for grp in chunks:
        # convert group back to lines for uniform shape
        text = "\n".join(grp)
        out.append(text.splitlines())
    return out


def _split_by_sentence(text: str, cap: int) -> list[list[str]]:
    """Split one long paragraph by sentence-ending punctuation, hard-cap fallback.

    Always emits pieces with len <= cap. If no sentence boundary exists within
    [cap//2, cap], falls back to a hard cap cut at exactly `cap` chars.
    """
    pieces: list[str] = []
    rest = text
    while len(rest) > cap:
        # search for sentence boundary strictly within [cap//2, cap] window
        # so we never cut beyond cap
        window_end = cap  # hard ceiling
        window = rest[:window_end]
        cut = -1
        for p in SENT_END_PUNCTS:
            # last punct at position <= cap
            i = window.rfind(p)
            if i >= cap // 2 and i > cut:
                cut = i
        if cut < 0:
            # no sentence boundary in [cap//2, cap] — hard cap
            cut = cap - 1
        pieces.append(rest[: cut + 1].rstrip())
        rest = rest[cut + 1 :].lstrip()
    if rest:
        pieces.append(rest)
    return [p.splitlines() for p in pieces]


def chunk_markdown(
    text: str,
    source_file: str = "",
    source_pmid: Optional[str] = None,
) -> list[MarkdownChunk]:
    """V2 hierarchical chunker.

    Returns list of MarkdownChunk, one per heading-level slice.
    """
    doc_id = _doc_slug(source_file)
    lines = text.splitlines()
    nodes: list[HeadingNode] = []  # ordered list of heading nodes
    in_code_fence = False
    cur_node: Optional[HeadingNode] = None
    pre_heading_buffer: list[str] = []  # lines before first heading (preface / abstract)

    def _flush_pre_heading() -> None:
        """Emit a synthetic level=0 node for preface/abstract (text before first heading)."""
        nonlocal pre_heading_buffer, cur_node
        # keep non-empty lines
        kept = [ln for ln in pre_heading_buffer if ln.strip()]
        pre_heading_buffer = []
        if not kept:
            return
        # build a synthetic node
        node = HeadingNode(
            level=0,
            section_number="",
            section_title="preface",
            heading_path="preface",
            path="/preface",
            heading_chain=["preface"],
            parent_id=None,
            body_lines=kept,
        )
        nodes.append(node)
        cur_node = node

    def _new_node(level: int, sec_num: str, title: str, h1_label: str, path: str, heading_chain: list[str], parent_chunk_id: Optional[str]) -> HeadingNode:
        heading_path = " > ".join(heading_chain)
        return HeadingNode(
            level=level,
            section_number=sec_num,
            section_title=_strip_star(title),
            heading_path=heading_path,
            path=path,
            heading_chain=list(heading_chain),
            parent_id=parent_chunk_id,
        )

    # state for path / chain / parent_id
    cur_h1: Optional[HeadingNode] = None
    cur_h2: Optional[HeadingNode] = None
    cur_h3: Optional[HeadingNode] = None

    for raw_line in lines:
        line = raw_line.rstrip()

        # code fence toggle
        if CODE_FENCE_RE.match(line):
            in_code_fence = not in_code_fence
            # code fence content belongs to current node's body (if any), else pre_heading_buffer
            target = cur_node.body_lines if cur_node else pre_heading_buffer
            target.append(line)
            continue
        if in_code_fence:
            target = cur_node.body_lines if cur_node else pre_heading_buffer
            target.append(line)
            continue

        # heading detection (try H3 first, then H2, then H1)
        matched_level: Optional[int] = None
        for lvl in (3, 2, 1):
            m = HEADING_PATTERNS[lvl].match(line)
            if m:
                payload = m.group("payload").strip()
                sec_num, title = _parse_section_num(payload)
                # build path / chain / parent_id based on level
                if lvl == 1:
                    if not nodes and pre_heading_buffer:
                        _flush_pre_heading()
                    # build Ch-style label for h1 path segment
                    if sec_num:
                        h1_seg = f"ch{sec_num}"
                        h1_label = f"Ch{sec_num} {title}".strip()
                    else:
                        h1_seg = f"h1_{len([n for n in nodes if n.level == 1]) + 1}"
                        h1_label = title
                    node = _new_node(
                        level=1,
                        sec_num=sec_num,
                        title=title,
                        h1_label=h1_label,
                        path=f"/{h1_seg}",
                        heading_chain=[h1_label],
                        parent_chunk_id=None,
                    )
                    cur_h1 = node
                    cur_h2 = None
                    cur_h3 = None
                elif lvl == 2:
                    h1_seg = cur_h1.path.lstrip("/") if cur_h1 else "h1_orphan"
                    h2_seg = sec_num if sec_num else f"h2_{len([n for n in nodes if n.level == 2 and n.parent_id == (cur_h1.chunk_ids[0] if cur_h1 and cur_h1.chunk_ids else None)]) + 1}"
                    path = f"/{h1_seg}/{h2_seg}"
                    parent_id = cur_h1.chunk_ids[0] if cur_h1 and cur_h1.chunk_ids else None
                    chain = (cur_h1.heading_chain + [f"{sec_num} {title}".strip()]) if cur_h1 else [f"{sec_num} {title}".strip()]
                    node = _new_node(
                        level=2,
                        sec_num=sec_num,
                        title=title,
                        h1_label=cur_h1.heading_path if cur_h1 else "",
                        path=path,
                        heading_chain=chain,
                        parent_chunk_id=parent_id,
                    )
                    cur_h2 = node
                    cur_h3 = None
                else:  # lvl == 3
                    if cur_h2:
                        parent_path = cur_h2.path
                        parent_id = cur_h2.chunk_ids[0] if cur_h2.chunk_ids else None
                        chain = cur_h2.heading_chain + [f"{sec_num} {title}".strip()]
                    elif cur_h1:
                        parent_path = cur_h1.path
                        parent_id = cur_h1.chunk_ids[0] if cur_h1.chunk_ids else None
                        chain = cur_h1.heading_chain + [f"{sec_num} {title}".strip()]
                    else:
                        parent_path = ""
                        parent_id = None
                        chain = [f"{sec_num} {title}".strip()]
                    h3_seg = sec_num if sec_num else f"h3_orphan"
                    path = f"{parent_path}/{h3_seg}" if parent_path else f"/{h3_seg}"
                    node = _new_node(
                        level=3,
                        sec_num=sec_num,
                        title=title,
                        h1_label="",
                        path=path,
                        heading_chain=chain,
                        parent_chunk_id=parent_id,
                    )
                    cur_h3 = node
                nodes.append(node)
                cur_node = node
                matched_level = lvl
                break
        if matched_level is not None:
            continue

        # body line (NOT a heading) — append to current node's body, or pre_heading_buffer
        target = cur_node.body_lines if cur_node else pre_heading_buffer
        # HR: kept as body (no special handling per L decision)
        # blockquote: strip leading `>` and keep as body line
        if BLOCKQUOTE_RE.match(line):
            stripped = BLOCKQUOTE_RE.sub("", line, count=1)
            target.append(stripped)
        else:
            target.append(line)

    # tail: any remaining pre_heading_buffer
    if pre_heading_buffer and not nodes:
        _flush_pre_heading()

    # ----- phase 2: build chunk_ids, then recompute parent_id from path, then child/sibling -----
    # Pass 2a: assign chunk_ids based on body size (single or p1..pN).
    for node in nodes:
        body_lines = node.body_lines
        body_text = "\n".join(body_lines).strip()
        if not body_text:
            node.chunk_ids = [_make_chunk_id(doc_id, node, "p1")]
            continue
        if len(body_text) <= MAX_CHUNK_CHARS:
            node.chunk_ids = [_make_chunk_id(doc_id, node, "p1")]
        else:
            parts = _split_oversize_body(body_lines, MAX_CHUNK_CHARS)
            node.chunk_ids = [_make_chunk_id(doc_id, node, f"p{i+1}") for i in range(len(parts))]
            node._body_parts = parts

    # Pass 2b: recompute parent_id from path (the path is the stable truth).
    # parent = node whose path == this node's parent path.
    path_to_node: dict[str, HeadingNode] = {n.path: n for n in nodes}
    for n in nodes:
        # derive parent path = node.path minus its own last segment
        segs = [s for s in n.path.split("/") if s]
        if len(segs) <= 1:
            # level 1 (or preface) has no parent in tree
            n.parent_id = None
        else:
            parent_path = "/" + "/".join(segs[:-1])
            parent_node = path_to_node.get(parent_path)
            n.parent_id = parent_node.chunk_ids[0] if parent_node else None

    # Pass 2c: child_ids / sibling_ids for *heading nodes*.
    # group nodes by (parent_id, level) — siblings must be at same level AND same parent.
    # (in V2, every node has exactly one parent path, so level consistency is implied
    #  for properly-nested docs; we still use level as safety.)
    by_parent: dict[Optional[str], list[HeadingNode]] = {}
    for n in nodes:
        by_parent.setdefault(n.parent_id, []).append(n)

    for n in nodes:
        my_id = n.chunk_ids[0]
        my_kids = by_parent.get(my_id, [])
        n.child_ids = [k.chunk_ids[0] for k in my_kids]
        # siblings: same parent, same level, exclude self
        sibs = [s for s in by_parent.get(n.parent_id, []) if s is not n and s.level == n.level]
        n.sibling_ids = [s.chunk_ids[0] for s in sibs]

    # ----- phase 3: emit MarkdownChunk list -----
    out: list[MarkdownChunk] = []
    for n in nodes:
        body_lines = n.body_lines
        if not "\n".join(body_lines).strip():
            # empty body — emit one empty chunk so the heading is in the tree
            out.append(_make_chunk(doc_id, n, body_lines, "p1"))
            continue
        if len(n.chunk_ids) == 1:
            out.append(_make_chunk(doc_id, n, body_lines, "p1"))
        else:
            parts = n._body_parts  # type: ignore[attr-defined]
            for i, part_lines in enumerate(parts, start=1):
                out.append(_make_chunk(doc_id, n, part_lines, f"p{i}"))

    # clean up stash
    for n in nodes:
        if hasattr(n, "_body_parts"):
            delattr(n, "_body_parts")

    return out


def _make_chunk_id(doc_id: str, node: HeadingNode, part_label: str) -> str:
    """Compose a readable chunk_id like
       `<doc_id>__<path_segments>__p<N>`. Path segments (e.g. ch5/5.1/5.1.1)
       are joined with single underscores and prefixed with the doc_id using a
       double underscore for clear visual separation:
         review_ai_fall_elderly__ch5__5.1__5.1.1__p1
    """
    parts = [doc_id]
    for p in node.path.lstrip("/").split("/"):
        if p:
            parts.append(p)
    parts.append(part_label)
    return "__".join(parts)


def _make_chunk(
    doc_id: str, node: HeadingNode, body_lines: list[str], part_label: str
) -> MarkdownChunk:
    text = "\n".join(body_lines).strip()
    chunk_id = _make_chunk_id(doc_id, node, part_label)
    return MarkdownChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        level=node.level,
        path=node.path,
        parent_id=node.parent_id,
        child_ids=list(node.child_ids),
        sibling_ids=list(node.sibling_ids),
        heading_chain=list(node.heading_chain),
        section_number=node.section_number,
        section_title=node.section_title,
        text=text,
        char_count=len(text),
        source_file="",  # filled by chunk_markdown_file wrapper
    )


def chunk_markdown_file(path: str | Path, source_pmid: Optional[str] = None) -> list[MarkdownChunk]:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="ignore")
    chunks = chunk_markdown(text, source_file=str(p), source_pmid=source_pmid)
    for c in chunks:
        c.source_file = str(p)
    return chunks


# ============ V3: heading-aware for plain text (no markdown) ============
# Used as fallback for .txt files that have no `#` headings.
# Heuristics: detect structural section headings from line patterns commonly
# found in PubMed-style plain-text articles.

# V3: Detect IMRAD / numbered / ALL-CAPS headings in plain-text sources (no markdown syntax).
# Uses regex byte literals; testing separately avoids raw-string escape pitfalls.
_IMRAD_HEADS = re.compile(
    "^(?P<payload>(Abstract|Background|Introduction|Methods?|Materials[ ]+and[ ]+Methods|Results|Discussion|Conclusions?|References|Acknowledg(?:e?)ments?|Limitations|Funding|Ethics|Supplementary))$",
    re.IGNORECASE,
)
_IMRAD_HEADS_COLON = re.compile(
    "^(?P<payload>(Abstract|Background|Introduction|Methods?|Materials[ ]+and[ ]+Methods|Results|Discussion|Conclusions?|References|Acknowledg(?:e?)ments?|Limitations|Funding))[ ]*[.:,-]$",
    re.IGNORECASE,
)
_NUMBERED_HEAD = re.compile(
    "^[0-9]+(?:[.][0-9]+){0,2}[.]?[ ]+(?P<payload>[A-Z][^.]{2,80})$"
)
_ALLCAPS_HEAD = re.compile(
    "^(?P<payload>[A-Z][A-Z0-9 ,:'\-]{4,80})$"
)
_PLAIN_TEXT_HEADING_PATTERNS: list[tuple[int, re.Pattern]] = [
    (1, _IMRAD_HEADS),
    (1, _IMRAD_HEADS_COLON),
    (2, _NUMBERED_HEAD),
    (2, _ALLCAPS_HEAD),
]


def chunk_plain_text(text: str, source_file: str = "", max_chars: int = MAX_CHUNK_CHARS) -> list[MarkdownChunk]:
    """V3 fallback chunker for plain-text sources (no markdown headings).

    Detects IMRAD sections + numbered + ALL-CAPS headings via regex, then
    splits body text into <max_chars chunks. Same HeadingNode tree structure
    as chunk_markdown so downstream heading_match / tree-aware search work
    uniformly across .md and .txt sources.
    """
    doc_id = _doc_slug(source_file) if source_file else "plain_text"
    lines = text.splitlines()
    nodes: list[HeadingNode] = []
    cur_node: Optional[HeadingNode] = None
    pre_heading_buffer: list[str] = []

    def _section_number(payload: str, level: int) -> tuple[str, str]:
        if level == 2:
            m = re.match(r"^(\d+(?:\.\d+){0,2})\.?\s+(.*)$", payload)
            if m:
                return m.group(1), m.group(2).strip()
        return "", payload.strip()

    def _make_path(level: int, sec_num: str, parent_path: str) -> str:
        seg = sec_num.lower().replace(".", "_") if sec_num else f"h{level}_{len([n for n in nodes if n.level == level]) + 1}"
        if parent_path:
            return f"{parent_path}/{seg}"
        return f"/{seg}"

    for raw_line in lines:
        line = raw_line.rstrip()
        matched_level: Optional[int] = None
        payload = ""
        for lvl, pat in _PLAIN_TEXT_HEADING_PATTERNS:
            m = pat.match(line)
            if m:
                payload = m.group("payload").strip()
                # sanity: line must be ALL the pattern (no trailing body)
                if len(payload) < 200:  # safety cap
                    matched_level = lvl
                    break
        if matched_level is not None:
            sec_num, title = _section_number(payload, matched_level)
            if cur_node is None and matched_level == 1:
                # emit preface for stuff before first IMRAD header
                _kept = [ln for ln in pre_heading_buffer if ln.strip()]
                if _kept:
                    pre_node = HeadingNode(
                        level=0, section_number="", section_title="preface",
                        heading_path="preface", path="/preface",
                        heading_chain=["preface"], parent_id=None,
                        body_lines=_kept,
                    )
                    nodes.append(pre_node)
            parent_path = "/preface" if (cur_node is None and matched_level == 1) else (
                cur_node.path if (cur_node and matched_level > cur_node.level) else (
                    # walk up: find closest ancestor with lower level
                    _ancestor_path(nodes, cur_node, matched_level) if cur_node else ""
                )
            )
            parent_id = _ancestor_chunk_id(nodes, cur_node, matched_level) if cur_node else None
            path = _make_path(matched_level, sec_num, parent_path)
            chain = _build_chain(nodes, cur_node, matched_level) + [f"{sec_num} {title}".strip() if sec_num else title]
            node = HeadingNode(
                level=matched_level,
                section_number=sec_num,
                section_title=_strip_star(title),
                heading_path=" > ".join(chain),
                path=path,
                heading_chain=chain,
                parent_id=parent_id,
            )
            nodes.append(node)
            cur_node = node
            pre_heading_buffer = []
            continue
        target = cur_node.body_lines if cur_node else pre_heading_buffer
        target.append(line)

    if cur_node is None and pre_heading_buffer:
        _kept = [ln for ln in pre_heading_buffer if ln.strip()]
        if _kept:
            pre_node = HeadingNode(
                level=0, section_number="", section_title="preface",
                heading_path="preface", path="/preface",
                heading_chain=["preface"], parent_id=None,
                body_lines=_kept,
            )
            nodes.append(pre_node)

    # emit MarkdownChunks from nodes (same logic as chunk_markdown phase 2/3)
    out: list[MarkdownChunk] = []
    for n in nodes:
        body_text = "\n".join(n.body_lines).strip()
        if not body_text:
            out.append(_make_chunk(doc_id, n, n.body_lines, "p1"))
            continue
        if len(body_text) <= max_chars:
            out.append(_make_chunk(doc_id, n, n.body_lines, "p1"))
        else:
            parts = _split_oversize_body(n.body_lines, max_chars)
            for i, part_lines in enumerate(parts, start=1):
                out.append(_make_chunk(doc_id, n, part_lines, f"p{i}"))
    return out


# helpers for chunk_plain_text tree construction
def _ancestor_path(nodes: list[HeadingNode], cur: HeadingNode, target_level: int) -> str:
    """Find path of closest ancestor with level < target_level, scanning nodes backwards."""
    for n in reversed(nodes):
        if n.level < target_level:
            return n.path
    return ""


def _ancestor_chunk_id(nodes: list[HeadingNode], cur: HeadingNode, target_level: int) -> Optional[str]:
    """Find chunk_id of closest ancestor with level < target_level."""
    for n in reversed(nodes):
        if n.level < target_level:
            return _make_chunk_id(_doc_slug(""), n, "p1")
    return None


def _build_chain(nodes: list[HeadingNode], cur: HeadingNode, target_level: int) -> list[str]:
    """Build heading_chain up to target_level (exclusive)."""
    chain: list[str] = []
    for n in reversed(nodes):
        if n.level < target_level:
            chain = list(reversed(n.heading_chain)) + chain
    return list(dict.fromkeys(chain))  # dedupe preserving order


# ============ Canonical entry point (IMPLEMENTATION.md §1) ============
# The chunker's single canonical input. `doc` is a canonical document dict (see
# corpus/canonical.py). Heading nodes define the tree; every non-heading node
# becomes one MarkdownChunk carrying its ancestor heading chain, so downstream
# heading_match / tree-aware retrieval work identically to the markdown path.

def chunk_canonical(doc: dict) -> list[MarkdownChunk]:
    """Convert a canonical document dict into MarkdownChunks.

    One chunk per non-heading node; heading nodes contribute their title to the
    heading_chain / heading_path of their descendants. Oversize node text is
    split with the same MAX_CHUNK_CHARS packing used for markdown.
    """
    nodes = doc.get("nodes", [])
    by_id = {n["node_id"]: n for n in nodes}
    doc_id = doc.get("doc_id", "doc")
    source_file = doc.get("source_file", "")

    def _chain(node: dict) -> list[str]:
        chain: list[str] = []
        cur = node
        # walk up parent links, collecting heading titles
        seen = set()
        while cur is not None and cur["node_id"] not in seen:
            seen.add(cur["node_id"])
            if cur["kind"] == "heading" and cur is not node:
                chain.append(cur["title"])
            pid = cur.get("parent_id")
            cur = by_id.get(pid) if pid else None
        return list(reversed(chain))

    out: list[MarkdownChunk] = []
    for node in nodes:
        if node["kind"] == "heading":
            continue
        text = node.get("text", "")
        if not text.strip():
            continue
        chain = _chain(node)
        heading_path = " > ".join(chain)
        level = len(chain)
        base_path = node.get("path", "")
        pieces = [text] if len(text) <= MAX_CHUNK_CHARS else [
            "\n".join(grp) for grp in _split_oversize_body(text.split("\n"), MAX_CHUNK_CHARS)
        ]
        for idx, piece in enumerate(pieces, start=1):
            piece = piece.strip()
            if not piece:
                continue
            chunk_id = f"{doc_id}__{node['node_id']}__p{idx}"
            out.append(MarkdownChunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                level=level,
                path=base_path,
                parent_id=node.get("parent_id"),
                child_ids=[],
                sibling_ids=[],
                heading_chain=chain,
                section_number="",
                section_title="",
                text=piece,
                char_count=len(piece),
                source_file=source_file,
            ))
    return out


def write_jsonl(chunks: list[MarkdownChunk], out_path: str | Path) -> int:
    """Write chunks as JSONL (one chunk per line). Returns count."""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for c in chunks:
            d = asdict(c)
            # dataclass list fields are already list; json handles them
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return len(chunks)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: chunker_markdown.py <file.md> [--jsonl out.jsonl]")
        sys.exit(1)
    file_path = sys.argv[1]
    args = sys.argv[2:]
    jsonl_out = None
    if "--jsonl" in args:
        idx = args.index("--jsonl")
        jsonl_out = args[idx + 1] if idx + 1 < len(args) else None
    chunks = chunk_markdown_file(file_path)
    print(f"# {len(chunks)} chunks from {file_path}\n")
    for c in chunks[:5]:
        print(json.dumps(asdict(c), ensure_ascii=False, indent=2))
        print("---")
    if len(chunks) > 5:
        print(f"... +{len(chunks) - 5} more chunks")
        lens = [c.char_count for c in chunks]
        print(
            f"\nchunk text length: min={min(lens)}, max={max(lens)}, "
            f"median={sorted(lens)[len(lens)//2]}"
        )
    if jsonl_out:
        n = write_jsonl(chunks, jsonl_out)
        print(f"\n✓ wrote {n} chunks to {jsonl_out}")