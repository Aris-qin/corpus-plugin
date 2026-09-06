"""corpus.canonical — canonical document schema + adapters (IMPLEMENTATION.md §1).

All input formats are normalised into a single ``.canon.json`` document; the
chunker only ever consumes canonical documents. Nodes are traceable back to the
source, and re-parsing unchanged content yields identical node ids.

This module owns:
  * the frozen data model (``CanonicalDocument`` / ``Node``) and its validation
  * ``node_id`` derivation (``nd_`` + sha256 prefix, occurrence-based path, with
    collision escalation)
  * tree-invariant enforcement (parent exists, path consistency, strictly
    increasing ordinal, heading gap compression, error-level warnings rejected)
  * legacy Markdown / plain-text adapters that produce the same canonical shape
  * ``canonicalize`` / ``canonicalize_file`` / ``process_raw`` skeletons

Anything marked "stage 5" (real docling/JATS parsing, embed + store) raises
``NotImplementedError`` with a stable message; the data model and adapters below
are fully functional and are what the §1 contract tests exercise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_VERSION = "1.0"

# kind enumeration — exactly the 9 kinds frozen by the contract.
KINDS = (
    "heading",
    "paragraph",
    "table",
    "formula",
    "caption",
    "list",
    "footnote",
    "inline_formula",
    "figure",
)

WARNING_LEVELS = ("info", "warn", "error")

_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# --------------------------------------------------------------------------- #
# exceptions
# --------------------------------------------------------------------------- #

class CanonicalError(ValueError):
    """Base class for canonical-schema violations."""


class InvalidNodeError(CanonicalError):
    """A node violates its per-kind field contract."""


class TreeInvariantError(CanonicalError):
    """The node list violates a tree invariant."""


class NodeIdCollision(CanonicalError):
    """Two distinct nodes resolve to the same node_id even after escalation."""


# --------------------------------------------------------------------------- #
# text normalisation
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
_MD_INLINE_RE = re.compile(r"[*_`~]+")


def normalize_text(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip ends.

    This is the canonical text-normalisation used both for ``text`` fields and
    for the ``text_normalized`` component of the node_id (so id stability does
    not depend on incidental whitespace)."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


def _strip_heading_markup(payload: str) -> str:
    """Heading text = markup-stripped, whitespace-collapsed title."""
    return normalize_text(_MD_INLINE_RE.sub("", payload))


# --------------------------------------------------------------------------- #
# node model
# --------------------------------------------------------------------------- #

@dataclass
class Node:
    """A canonical node. ``node_id`` / ``path`` / ``ordinal`` / ``parent_id`` are
    filled by the builder; callers supply the semantic fields."""

    kind: str
    text: str = ""
    level: int = 0
    sec_num: Optional[str] = None
    title: str = ""
    ordinal: int = 0
    parent_id: Optional[str] = None
    path: str = ""
    locator: Optional[dict] = None
    attrs: dict = field(default_factory=dict)
    node_id: str = ""
    node_id_full: Optional[str] = None
    # internal-only, not serialised: occurrence-based path used for the id hash.
    canonical_path: str = ""

    def to_dict(self) -> dict:
        d = {
            "node_id": self.node_id,
            "kind": self.kind,
            "level": self.level,
            "sec_num": self.sec_num,
            "title": self.title,
            "text": self.text,
            "ordinal": self.ordinal,
            "parent_id": self.parent_id,
            "path": self.path,
            "locator": self.locator,
            "attrs": self.attrs,
        }
        if self.node_id_full:
            d["node_id_full"] = self.node_id_full
        return d


@dataclass
class CanonicalDocument:
    schema_version: str
    doc_id: str
    content_hash: str
    source_file: str
    parser: str
    parser_version: str
    nodes: list[Node]
    parser_warnings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "doc_id": self.doc_id,
            "content_hash": self.content_hash,
            "source_file": self.source_file,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "nodes": [n.to_dict() for n in self.nodes],
            "parser_warnings": list(self.parser_warnings),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=False)


@dataclass
class ProcessResult:
    """Outcome of ``process_raw`` — records which entry path was taken so callers
    (and tests) can assert the adapter was used and the legacy chunker was not."""

    pmid: str
    source: str                       # "canon" | "adapter"
    adapter: Optional[str]            # "adapt_markdown" | "adapt_txt" | None
    document: CanonicalDocument
    used_legacy_chunker: bool = False
    generation: Optional[int] = None


# --------------------------------------------------------------------------- #
# node_id derivation
# --------------------------------------------------------------------------- #

def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def node_id_payload(content_hash: str, canonical_path: str, kind: str, text_normalized: str) -> str:
    """The exact byte payload hashed for a node_id (NUL-joined)."""
    return "\0".join((content_hash, canonical_path, kind, text_normalized))


def compute_node_id(
    content_hash: str,
    canonical_path: str,
    kind: str,
    text: str,
    *,
    length: int = 12,
    hash_fn: Callable[[str], str] = _sha256_hex,
) -> tuple[str, str]:
    """Return ``(node_id, full_hex)`` for a single node.

    node_id = ``nd_`` + first ``length`` hex of
    ``sha256(content_hash \\0 canonical_path \\0 kind \\0 text_normalized)``.
    ``ordinal`` and ``locator`` deliberately do not participate."""
    full = hash_fn(node_id_payload(content_hash, canonical_path, kind, normalize_text(text)))
    return "nd_" + full[:length], full


def assign_node_ids(
    nodes: list[Node],
    content_hash: str,
    *,
    hash_fn: Callable[[str], str] = _sha256_hex,
) -> None:
    """Assign ``node_id`` to every node, escalating the hash-prefix length on
    truncation collisions (12 → 16 → 20 hex) and writing ``node_id_full`` for any
    node that had to escalate. Two distinct nodes that still collide after 20 hex
    (or share a 12-hex id with a *different* full hash that cannot be separated)
    raise ``NodeIdCollision``."""
    fulls: dict[int, str] = {}
    for i, n in enumerate(nodes):
        fulls[i] = hash_fn(node_id_payload(content_hash, n.canonical_path, n.kind, normalize_text(n.text)))

    # Distinct nodes must have distinct full hashes; if two share a full hash they
    # are the *same* logical node, which the occurrence-based canonical_path must
    # already have made unique. A genuine full-hash tie is unrepresentable.
    for length in (12, 16, 20):
        prefixes: dict[str, str] = {}          # prefix -> full
        collided_prefixes: set[str] = set()
        for i, n in enumerate(nodes):
            pfx = fulls[i][:length]
            prev = prefixes.get(pfx)
            if prev is not None and prev != fulls[i]:
                collided_prefixes.add(pfx)
            prefixes[pfx] = fulls[i]
        if not collided_prefixes:
            escalated = length > 12
            for i, n in enumerate(nodes):
                n.node_id = "nd_" + fulls[i][:length]
                n.node_id_full = ("nd_" + fulls[i]) if escalated else None
            return

    raise NodeIdCollision(
        "node_id collision unresolved at 20 hex — distinct nodes share a hash prefix"
    )


# --------------------------------------------------------------------------- #
# tree building
# --------------------------------------------------------------------------- #

def _finalize_tree(raw_nodes: list[Node]) -> list[Node]:
    """Given nodes in document order with ``level`` set (headings 1..6, others 0)
    and a transient ``_raw_level`` on headings, assign parent_id/path/ordinal and
    the occurrence-based ``canonical_path``, applying heading gap compression.

    Non-heading blocks attach to the nearest preceding heading (or root)."""
    # 1) heading gap compression: recompute effective heading levels so they never
    #    skip (H1 -> H3 becomes H1 -> H2). Maintain a stack of raw levels.
    stack: list[Node] = []           # current heading ancestry (effective order)
    for n in raw_nodes:
        if n.kind == "heading":
            raw = getattr(n, "_raw_level", n.level)
            while stack and getattr(stack[-1], "_raw_level", stack[-1].level) >= raw:
                stack.pop()
            n.level = len(stack) + 1        # contiguous effective level
            n.parent_id = None              # resolved below via id, set marker now
            n._parent_node = stack[-1] if stack else None  # type: ignore[attr-defined]
            stack.append(n)
        else:
            n.level = 0
            n._parent_node = stack[-1] if stack else None  # type: ignore[attr-defined]

    # 2) assign ordinal + path (sibling index) + canonical_path (occurrence count).
    child_counter: dict[Optional[int], int] = {}
    # occurrence counter keyed by (parent canonical_path, kind, normalized text)
    occ_counter: dict[tuple[str, str, str], int] = {}
    for i, n in enumerate(raw_nodes):
        n.ordinal = i + 1
        parent = getattr(n, "_parent_node", None)
        pkey = id(parent) if parent is not None else None
        idx = child_counter.get(pkey, 0) + 1
        child_counter[pkey] = idx
        parent_path = parent.path if parent is not None else ""
        n.path = f"{parent_path}/{idx}"

        parent_cpath = parent.canonical_path if parent is not None else ""
        okey = (parent_cpath, n.kind, normalize_text(n.text))
        occ = occ_counter.get(okey, 0)
        occ_counter[okey] = occ + 1
        n.canonical_path = f"{parent_cpath}/{n.kind}:{occ}"

    # 3) resolve parent_id from parent node identity (ids assigned by caller after).
    return raw_nodes


def _link_parent_ids(nodes: list[Node]) -> None:
    """After ids are assigned, translate ``_parent_node`` markers to parent_id."""
    for n in nodes:
        parent = getattr(n, "_parent_node", None)
        n.parent_id = parent.node_id if parent is not None else None


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def validate_node(node: Node) -> None:
    """Enforce the per-kind ``text`` / ``attrs`` / ``level`` / ``title`` rules."""
    if node.kind not in KINDS:
        raise InvalidNodeError(f"unknown kind: {node.kind!r}")

    if node.locator is not None:
        loc = node.locator
        if not isinstance(loc, dict) or "char_start" not in loc or "char_end" not in loc:
            raise InvalidNodeError("locator must be null or {char_start,char_end}")
        cs, ce = loc["char_start"], loc["char_end"]
        if cs is not None and ce is not None:
            if not (isinstance(cs, int) and isinstance(ce, int)) or ce < cs:
                raise InvalidNodeError("locator must be a half-open [char_start,char_end)")

    if node.kind == "heading":
        if not (1 <= node.level <= 6):
            raise InvalidNodeError("heading level must be 1..6")
        if not node.title:
            raise InvalidNodeError("heading title must be non-empty")
        if node.text != node.title:
            raise InvalidNodeError("heading text must equal its collapsed title")
        return

    # non-heading:
    if node.level != 0:
        raise InvalidNodeError(f"{node.kind} level must be 0")
    if node.title != "":
        raise InvalidNodeError(f"{node.kind} title must be empty")

    if node.kind == "table":
        if not node.attrs.get("markdown"):
            raise InvalidNodeError("table attrs must include markdown")
        if not node.text or "|" not in node.text:
            raise InvalidNodeError("table text must be GFM markdown")
    elif node.kind in ("formula", "inline_formula"):
        if "latex" not in node.attrs or "display" not in node.attrs:
            raise InvalidNodeError(f"{node.kind} attrs must include latex/display")
        if not node.text:
            raise InvalidNodeError(f"{node.kind} text must be canonical LaTeX")
    elif node.kind == "figure":
        if "alt" not in node.attrs or "uri" not in node.attrs:
            raise InvalidNodeError("figure attrs must include alt/uri")
        if not node.text:
            raise InvalidNodeError("figure text must be alt or [figure]")
    elif node.kind == "list":
        if not node.text:
            raise InvalidNodeError("list text must be non-empty")
        for line in node.text.split("\n"):
            if not line.startswith("- "):
                raise InvalidNodeError("each list line must start with '- '")
    elif node.kind in ("caption", "footnote"):
        if not node.text:
            raise InvalidNodeError(f"{node.kind} text must be non-empty")


def validate_document(doc: CanonicalDocument) -> None:
    """Full document validation: header fields, per-node rules, tree invariants,
    and error-level warning rejection. Raises on any violation."""
    if doc.schema_version != SCHEMA_VERSION:
        raise CanonicalError(f"schema_version must be {SCHEMA_VERSION}")
    if not _CONTENT_HASH_RE.match(doc.content_hash):
        raise CanonicalError("content_hash must be 'sha256:<64hex>'")

    for w in doc.parser_warnings:
        if w.get("level") not in WARNING_LEVELS:
            raise CanonicalError(f"invalid warning level: {w.get('level')!r}")
        if w.get("level") == "error":
            raise TreeInvariantError(f"error-level parser warning rejects ingest: {w.get('msg')}")

    ids = {n.node_id for n in doc.nodes}
    if len(ids) != len(doc.nodes):
        raise NodeIdCollision("duplicate node_id in document")

    prev_ordinal = 0
    by_id = {n.node_id: n for n in doc.nodes}
    for n in doc.nodes:
        validate_node(n)
        # ordinal strictly increasing
        if n.ordinal <= prev_ordinal:
            raise TreeInvariantError(f"ordinal not strictly increasing at {n.node_id}")
        prev_ordinal = n.ordinal
        # parent exists or null
        if n.parent_id is not None and n.parent_id not in by_id:
            raise TreeInvariantError(f"parent_id {n.parent_id} does not exist")
        # path consistent with parent chain
        parent = by_id.get(n.parent_id) if n.parent_id else None
        parent_path = parent.path if parent else ""
        if not n.path.startswith(parent_path + "/"):
            raise TreeInvariantError(f"path {n.path} inconsistent with parent {parent_path}")
        # a path is parent_path + exactly one more segment
        if n.path[len(parent_path):].count("/") != 1:
            raise TreeInvariantError(f"path {n.path} is not a direct child of {parent_path}")


# --------------------------------------------------------------------------- #
# document assembly
# --------------------------------------------------------------------------- #

def build_document(
    raw_nodes: list[Node],
    *,
    doc_id: str,
    content_hash: str,
    source_file: str,
    parser: str,
    parser_version: str,
    warnings: Optional[list[dict]] = None,
    hash_fn: Callable[[str], str] = _sha256_hex,
    validate: bool = True,
) -> CanonicalDocument:
    """Turn a flat, document-order node list into a validated CanonicalDocument."""
    _finalize_tree(raw_nodes)
    assign_node_ids(raw_nodes, content_hash, hash_fn=hash_fn)
    _link_parent_ids(raw_nodes)
    doc = CanonicalDocument(
        schema_version=SCHEMA_VERSION,
        doc_id=doc_id,
        content_hash=content_hash,
        source_file=source_file,
        parser=parser,
        parser_version=parser_version,
        nodes=raw_nodes,
        parser_warnings=list(warnings or []),
    )
    if validate:
        validate_document(doc)
    return doc


# --------------------------------------------------------------------------- #
# markdown adapter
# --------------------------------------------------------------------------- #

_H_RE = re.compile(r"^(#{1,6})\s+(?P<payload>.+?)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}.*$")
_FIGURE_RE = re.compile(r"^!\[(?P<alt>.*?)\]\((?P<uri>[^)]*)\)\s*$")
_FOOTNOTE_RE = re.compile(r"^\[\^(?P<ref>[^\]]+)\]:\s*(?P<body>.+?)\s*$")
_CAPTION_RE = re.compile(r"^(?P<label>(Figure|Fig\.|Table)\s+\S+)[.:]?\s*(?P<body>.*)$", re.IGNORECASE)
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?P<item>.+?)\s*$")
_BLOCK_FORMULA_RE = re.compile(r"^\$\$(?P<latex>.+?)\$\$\s*$")
_INLINE_ONLY_RE = re.compile(r"^\$(?P<latex>[^$]+?)\$\s*$")
_SEC_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$")


def _split_sec_num(title: str) -> tuple[Optional[str], str]:
    m = _SEC_NUM_RE.match(title)
    if m:
        return m.group(1), m.group(2).strip()
    return None, title


def adapt_markdown(text: str, *, source_file: str, content_hash: str) -> dict:
    """Legacy Markdown adapter — parse ``text`` into a canonical document dict.

    Recognises headings, GFM tables, block/inline formulas, images (figures),
    footnote definitions, captions (Figure/Table N …), lists and paragraphs.
    The chunker never sees the raw markdown; it flows through this single
    canonical entry point (parser=``legacy_md``)."""
    lines = text.split("\n")
    raw_nodes: list[Node] = []
    warnings: list[dict] = []
    i = 0
    n_lines = len(lines)

    def _add(node: Node) -> None:
        raw_nodes.append(node)

    while i < n_lines:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # heading
        m = _H_RE.match(line)
        if m:
            raw_level = len(m.group(1))
            payload = m.group("payload").strip()
            sec_num, title_rest = _split_sec_num(payload)
            title = _strip_heading_markup(title_rest if sec_num else payload)
            node = Node(kind="heading", level=raw_level, sec_num=sec_num, title=title, text=title)
            node._raw_level = raw_level  # type: ignore[attr-defined]
            _add(node)
            i += 1
            continue

        # block formula $$...$$ (single line)
        mf = _BLOCK_FORMULA_RE.match(stripped)
        if mf:
            latex = mf.group("latex").strip()
            _add(Node(kind="formula", text=latex, attrs={"latex": latex, "display": True}))
            i += 1
            continue

        # figure
        mfig = _FIGURE_RE.match(stripped)
        if mfig:
            alt = mfig.group("alt").strip()
            uri = mfig.group("uri").strip()
            if not alt:
                warnings.append({"level": "warn", "code": "figure_no_alt",
                                 "msg": f"figure without alt text: {uri}"})
                fig_text = "[figure]"
            else:
                fig_text = alt
            _add(Node(kind="figure", text=fig_text, attrs={"alt": alt, "uri": uri}))
            i += 1
            continue

        # footnote definition
        mfn = _FOOTNOTE_RE.match(line)
        if mfn:
            body = normalize_text(mfn.group("body"))
            _add(Node(kind="footnote", text=body,
                      attrs={"ref": mfn.group("ref"), "markdown": stripped}))
            i += 1
            continue

        # inline-only formula (a lone $x$ line)
        mi = _INLINE_ONLY_RE.match(stripped)
        if mi:
            latex = mi.group("latex").strip()
            _add(Node(kind="inline_formula", text=latex, attrs={"latex": latex, "display": False}))
            i += 1
            continue

        # table: current line looks like a row and next line is a separator
        if stripped.startswith("|") and i + 1 < n_lines and _TABLE_SEP_RE.match(lines[i + 1]):
            table_lines = []
            while i < n_lines and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].rstrip())
                i += 1
            markdown = "\n".join(table_lines)
            _add(Node(kind="table", text=markdown, attrs={"markdown": markdown}))
            continue

        # caption
        mc = _CAPTION_RE.match(stripped)
        if mc:
            _add(Node(kind="caption", text=normalize_text(stripped),
                      attrs={"label": mc.group("label"), "ref": mc.group("label")}))
            i += 1
            continue

        # list: consecutive list lines
        if _LIST_RE.match(line):
            items = []
            raw_md = []
            while i < n_lines and _LIST_RE.match(lines[i]):
                items.append(normalize_text(_LIST_RE.match(lines[i]).group("item")))
                raw_md.append(lines[i].rstrip())
                i += 1
            list_text = "\n".join(f"- {it}" for it in items)
            _add(Node(kind="list", text=list_text,
                      attrs={"items": items, "markdown": "\n".join(raw_md)}))
            continue

        # paragraph: gather consecutive non-blank, non-structural lines
        para_lines = []
        raw_md = []
        while i < n_lines:
            cur = lines[i]
            cs = cur.strip()
            if not cs:
                break
            if _H_RE.match(cur) or _FIGURE_RE.match(cs) or _FOOTNOTE_RE.match(cur) \
                    or _LIST_RE.match(cur) or _BLOCK_FORMULA_RE.match(cs) \
                    or (cs.startswith("|") and i + 1 < n_lines and _TABLE_SEP_RE.match(lines[i + 1])):
                break
            para_lines.append(cs)
            raw_md.append(cur.rstrip())
            i += 1
        para_text = normalize_text(" ".join(para_lines))
        if para_text:
            _add(Node(kind="paragraph", text=para_text, attrs={"markdown": "\n".join(raw_md)}))

    doc = build_document(
        raw_nodes,
        doc_id=Path(source_file).stem or "doc",
        content_hash=content_hash,
        source_file=source_file,
        parser="legacy_md",
        parser_version="1.0",
        warnings=warnings,
    )
    return doc.to_dict()


# --------------------------------------------------------------------------- #
# plain-text adapter
# --------------------------------------------------------------------------- #

_IMRAD_RE = re.compile(
    r"^(Abstract|Background|Introduction|Methods?|Materials and Methods|Results|"
    r"Discussion|Conclusions?|References|Acknowledgements?|Limitations|Funding)\s*[:.]?\s*$",
    re.IGNORECASE,
)
_NUM_HEAD_RE = re.compile(r"^(\d+(?:\.\d+){0,2})\.?\s+(?P<title>[A-Z][^.]{2,80})$")


def adapt_txt(text: str, *, source_file: str, content_hash: str) -> dict:
    """Legacy plain-text adapter (parser=``legacy_txt``).

    Detects IMRAD / numbered headings; everything else is a paragraph. Blank
    lines separate paragraphs."""
    lines = text.split("\n")
    raw_nodes: list[Node] = []
    para_buf: list[str] = []

    def _flush_para() -> None:
        nonlocal para_buf
        joined = normalize_text(" ".join(para_buf))
        para_buf = []
        if joined:
            raw_nodes.append(Node(kind="paragraph", text=joined))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            _flush_para()
            continue

        mi = _IMRAD_RE.match(stripped)
        mn = _NUM_HEAD_RE.match(stripped)
        if mi:
            _flush_para()
            title = _strip_heading_markup(mi.group(1))
            node = Node(kind="heading", level=1, sec_num=None, title=title, text=title)
            node._raw_level = 1  # type: ignore[attr-defined]
            raw_nodes.append(node)
        elif mn:
            _flush_para()
            title = _strip_heading_markup(mn.group("title"))
            node = Node(kind="heading", level=2, sec_num=mn.group(1), title=title, text=title)
            node._raw_level = 2  # type: ignore[attr-defined]
            raw_nodes.append(node)
        else:
            para_buf.append(stripped)
    _flush_para()

    doc = build_document(
        raw_nodes,
        doc_id=Path(source_file).stem or "doc",
        content_hash=content_hash,
        source_file=source_file,
        parser="legacy_txt",
        parser_version="1.0",
    )
    return doc.to_dict()


# --------------------------------------------------------------------------- #
# canonicalize entry points
# --------------------------------------------------------------------------- #

def content_hash_of(source: bytes) -> str:
    return "sha256:" + hashlib.sha256(source).hexdigest()


def _detect_fmt(source_file: str, fmt: str) -> str:
    if fmt != "auto":
        return fmt
    ext = Path(source_file).suffix.lower().lstrip(".")
    return ext or "txt"


def canonicalize(
    source: bytes,
    *,
    source_file: str,
    fmt: str = "auto",
    parser_version: str,
    doc_id: Optional[str] = None,
) -> dict:
    """Canonicalize raw bytes into a canonical document dict.

    Markdown and plain text are handled natively via the legacy adapters. Rich
    formats (pdf/docx/html/jats) require the docling / jats parsers wired in
    stage 5 and raise ``NotImplementedError`` here."""
    resolved = _detect_fmt(source_file, fmt)
    chash = content_hash_of(source)
    text = source.decode("utf-8", errors="replace")

    if resolved in ("md", "markdown"):
        d = adapt_markdown(text, source_file=source_file, content_hash=chash)
    elif resolved in ("txt", "text"):
        d = adapt_txt(text, source_file=source_file, content_hash=chash)
    else:
        raise NotImplementedError(
            f"stage 5: native {resolved} parser (docling/jats) not wired yet"
        )
    if doc_id:
        d["doc_id"] = doc_id
    d["parser_version"] = parser_version
    return d


def canonicalize_file(path: Path, *, output: Path, fmt: str = "auto") -> CanonicalDocument:
    """Canonicalize a file and atomically write ``<output>`` (temp + os.replace)."""
    path = Path(path)
    source = path.read_bytes()
    d = canonicalize(source, source_file=str(path), fmt=fmt, parser_version="1.0")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, output)
    return document_from_dict(d)


def canon_output_path(canon_root: Path, pmid: str, content_hash: str) -> Path:
    """`<canon_root>/<pmid>/<content_hash>.canon.json` (hash without the sha256: prefix)."""
    digest = content_hash.split(":", 1)[-1]
    return Path(canon_root) / str(pmid) / f"{digest}.canon.json"


def document_from_dict(d: dict) -> CanonicalDocument:
    nodes = []
    for nd in d.get("nodes", []):
        node = Node(
            kind=nd["kind"],
            text=nd.get("text", ""),
            level=nd.get("level", 0),
            sec_num=nd.get("sec_num"),
            title=nd.get("title", ""),
            ordinal=nd.get("ordinal", 0),
            parent_id=nd.get("parent_id"),
            path=nd.get("path", ""),
            locator=nd.get("locator"),
            attrs=nd.get("attrs", {}),
            node_id=nd.get("node_id", ""),
            node_id_full=nd.get("node_id_full"),
        )
        nodes.append(node)
    return CanonicalDocument(
        schema_version=d["schema_version"],
        doc_id=d["doc_id"],
        content_hash=d["content_hash"],
        source_file=d["source_file"],
        parser=d["parser"],
        parser_version=d["parser_version"],
        nodes=nodes,
        parser_warnings=d.get("parser_warnings", []),
    )


def process_raw(
    pmid,
    *,
    canon_path: Optional[Path] = None,
    legacy_path: Optional[Path] = None,
    generation: Optional[int] = None,
) -> ProcessResult:
    """Resolve the canonical document for ``pmid`` and hand it to the chunker.

    Prefers an existing ``.canon.json``; only when absent does it fall back to the
    legacy adapter (never to the old markdown chunker directly — the chunker has a
    single canonical entry point). The embed + store half is stage 5."""
    if canon_path is not None and Path(canon_path).exists():
        d = json.loads(Path(canon_path).read_text(encoding="utf-8"))
        doc = document_from_dict(d)
        validate_document(doc)
        return ProcessResult(pmid=str(pmid), source="canon", adapter=None,
                             document=doc, used_legacy_chunker=False, generation=generation)

    if legacy_path is not None:
        legacy_path = Path(legacy_path)
        text = legacy_path.read_text(encoding="utf-8", errors="replace")
        chash = content_hash_of(text.encode("utf-8"))
        ext = legacy_path.suffix.lower().lstrip(".")
        if ext in ("md", "markdown"):
            d = adapt_markdown(text, source_file=str(legacy_path), content_hash=chash)
            adapter = "adapt_markdown"
        else:
            d = adapt_txt(text, source_file=str(legacy_path), content_hash=chash)
            adapter = "adapt_txt"
        doc = document_from_dict(d)
        validate_document(doc)
        return ProcessResult(pmid=str(pmid), source="adapter", adapter=adapter,
                             document=doc, used_legacy_chunker=False, generation=generation)

    raise CanonicalError(f"process_raw({pmid}): neither canon_path nor legacy_path available")
