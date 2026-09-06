"""corpus.generation — chunk_id codec, generation manifest, re-chunk state machine.

Implements IMPLEMENTATION.md §2:

  * ``parse_chunk_id`` / ``encode_chunk_id`` — a *length-prefixed* v2 chunk id so
    that ``/``, ``_`` and ``__`` carry no structural meaning, plus a reader for
    legacy ``pmid__path__pN`` ids (which are read-only: ``legacy=True``,
    ``generation=None``). Parse → validate → round-trip.
  * ``generation_manifest`` DDL with a *partial* unique index enforcing at most
    one ``active`` generation per pmid, and the ``documents``/``chunks``
    ``generation`` columns the vec0 filter joins against.
  * a re-chunk state machine skeleton: ``begin_generation`` (BEGIN IMMEDIATE →
    ``building``), ``activate_generation`` (atomic old ``active`` → ``retired``,
    new → ``active``, ``documents.generation`` update), ``fail_generation`` and
    ``cleanup_orphan_building`` (crash recovery / 7-day retention window).

Wire format (after the fixed ``c2.<gen8>.<hash12>.<chunker8>.`` prefix)::

    <n>:<plen>:<path (exactly plen chars)>:<part>

``<n>`` is an integer field, ``<plen>`` is the byte length of ``path`` so the
path may itself contain ``:``/``/``/``_`` without ambiguity, and ``<part>`` is
the terminal remainder. Example (from the contract, with a path that itself
contains a colon)::

    c2.g000001a.ab12cd34ef56.ab12cd34.2:5:1/2:3:p1
        gen8      hash12       chunker8 n plen path  part
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------- #
# chunk_id codec
# --------------------------------------------------------------------------- #

# Structured prefix of the new id. gen8 is base36, hash12 / chunker8 are hex.
NEW_RE = re.compile(r"^c2\.([0-9a-z]{8})\.([0-9a-f]{12})\.([0-9a-f]{8})\.(\d+):")

# Legacy readable id: ``<pmid>__<path segments>__p<N>``. Only ever read; never
# written (see ``require_writable``).
LEGACY_RE = re.compile(r"^(?P<pmid>[^.].*?)__(?P<path>.+)__p(?P<part>\d+)$")


class InvalidChunkId(ValueError):
    """A string is neither a well-formed v2 id nor a legacy id."""


class LegacyChunkIdNotWritable(ValueError):
    """Attempted to (re-)write a legacy chunk id — compat reads only."""


@dataclass(frozen=True)
class ChunkId:
    raw: str
    legacy: bool
    generation: Optional[int] = None      # decoded from gen8 (base36), None for legacy
    gen8: Optional[str] = None
    content_hash12: Optional[str] = None
    chunker8: Optional[str] = None
    n: Optional[int] = None
    path: Optional[str] = None
    part: Optional[str] = None
    legacy_pmid: Optional[str] = None


def _b36_encode(value: int, width: int = 8) -> str:
    if value < 0:
        raise ValueError("generation must be non-negative")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        s = "0"
    else:
        out = []
        v = value
        while v:
            v, r = divmod(v, 36)
            out.append(digits[r])
        s = "".join(reversed(out))
    if len(s) > width:
        raise ValueError(f"generation {value} does not fit in {width} base36 chars")
    return s.rjust(width, "0")


def _b36_decode(s: str) -> int:
    return int(s, 36)


def chunker8(name: str, version: str) -> str:
    """The 8-hex chunker fingerprint: ``sha256(name+version)[:8]``."""
    return hashlib.sha256(f"{name}{version}".encode("utf-8")).hexdigest()[:8]


def encode_chunk_id(
    *,
    generation: int,
    content_hash: str,
    chunker_fp: str,
    n: int,
    path: str,
    part: str,
) -> str:
    """Encode a v2 chunk id. ``content_hash`` may carry the ``sha256:`` prefix.

    The path is length-prefixed so ``/``/``_``/``__``/``:`` inside it are inert."""
    gen8 = _b36_encode(generation)
    digest = content_hash.split(":", 1)[-1]
    hash12 = digest[:12]
    if not re.fullmatch(r"[0-9a-f]{12}", hash12):
        raise InvalidChunkId(f"content_hash prefix not 12 hex: {hash12!r}")
    if not re.fullmatch(r"[0-9a-f]{8}", chunker_fp):
        raise InvalidChunkId(f"chunker fingerprint not 8 hex: {chunker_fp!r}")
    if int(n) < 0:
        raise InvalidChunkId("n must be non-negative")
    return f"c2.{gen8}.{hash12}.{chunker_fp}.{int(n)}:{len(path)}:{path}:{part}"


def parse_chunk_id(s: str) -> ChunkId:
    """Parse a v2 or legacy chunk id. Raises ``InvalidChunkId`` otherwise."""
    if s.startswith("c2."):
        m = NEW_RE.match(s)
        if not m:
            raise InvalidChunkId(f"malformed v2 chunk id prefix: {s!r}")
        gen8, hash12, ck8, n_str = m.group(1), m.group(2), m.group(3), m.group(4)
        rest = s[m.end():]                        # after "<n>:"
        # rest == "<plen>:<path><:><part>"
        plen_str, _, after_len = rest.partition(":")
        if not plen_str.isdigit() or not _:
            raise InvalidChunkId(f"missing path length prefix: {s!r}")
        plen = int(plen_str)
        if len(after_len) < plen + 1:
            raise InvalidChunkId(f"path length {plen} overruns id: {s!r}")
        path = after_len[:plen]
        sep_and_part = after_len[plen:]
        if not sep_and_part.startswith(":"):
            raise InvalidChunkId(f"path not terminated by ':' : {s!r}")
        part = sep_and_part[1:]
        cid = ChunkId(
            raw=s,
            legacy=False,
            generation=_b36_decode(gen8),
            gen8=gen8,
            content_hash12=hash12,
            chunker8=ck8,
            n=int(n_str),
            path=path,
            part=part,
        )
        # round-trip self-check: parse → encode must reproduce the input exactly.
        if encode_chunk_id(generation=cid.generation, content_hash=hash12,
                           chunker_fp=ck8, n=cid.n, path=path, part=part) != s:
            raise InvalidChunkId(f"chunk id does not round-trip: {s!r}")
        return cid

    m = LEGACY_RE.fullmatch(s)
    if m:
        return ChunkId(
            raw=s,
            legacy=True,
            generation=None,
            n=None,
            path=m.group("path"),
            part="p" + m.group("part"),
            legacy_pmid=m.group("pmid"),
        )

    raise InvalidChunkId(f"not a chunk id: {s!r}")


def require_writable(cid: ChunkId) -> ChunkId:
    """Guard used on any write path — legacy ids are read-only (§2: 拒写)."""
    if cid.legacy:
        raise LegacyChunkIdNotWritable(f"legacy chunk id is read-only: {cid.raw}")
    return cid


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

GENERATION_MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS generation_manifest(
 generation INTEGER PRIMARY KEY, pmid TEXT NOT NULL, content_hash TEXT NOT NULL,
 chunker_name TEXT NOT NULL, chunker_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('building','active','retired','failed')),
 created_at TEXT NOT NULL, activated_at TEXT, retired_at TEXT,
 chunk_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS generation_one_active_per_pmid
 ON generation_manifest(pmid) WHERE status='active';
"""


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def install_generation_schema(conn) -> None:
    """Idempotently install the manifest table + partial unique index and add the
    ``generation`` columns the vec0 filter joins against (documents + chunks).

    The contract's executable ``vector_search`` joins ``chunks c`` on
    ``g.generation=c.generation``; the ALTER on ``documents`` records each doc's
    active generation. Both columns are added here so the filter is consistent."""
    conn.executescript(GENERATION_MANIFEST_DDL)
    for table in ("documents", "chunks"):
        try:
            if not _has_column(conn, table, "generation"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN generation INTEGER")
        except Exception:
            # table may not exist yet in a fresh db; schema creator adds the column.
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS documents_generation_idx ON documents(pmid,generation)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS chunks_generation_idx ON chunks(pmid,generation)"
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# re-chunk state machine
# --------------------------------------------------------------------------- #

class GenerationError(RuntimeError):
    """A generation transition violated the state machine."""


def _now(conn) -> str:
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).fetchone()[0]


def begin_generation(conn, *, pmid: str, content_hash: str,
                     chunker_name: str, chunker_version: str) -> int:
    """Reserve a new ``building`` generation under BEGIN IMMEDIATE and return its id.

    The caller then writes chunks/vectors tagged with this generation and calls
    ``activate_generation`` to switch it live (or ``fail_generation`` on error)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM generation_manifest"
        ).fetchone()
        gen = int(row[0])
        now = _now(conn)
        conn.execute(
            """INSERT INTO generation_manifest
               (generation, pmid, content_hash, chunker_name, chunker_version,
                status, created_at, chunk_count)
               VALUES (?,?,?,?,?, 'building', ?, 0)""",
            (gen, pmid, content_hash, chunker_name, chunker_version, now),
        )
        conn.execute("COMMIT")
        return gen
    except Exception:
        conn.execute("ROLLBACK")
        raise


def activate_generation(conn, generation: int, *, chunk_count: Optional[int] = None) -> None:
    """Atomically switch ``generation`` live: retire the current ``active`` gen for
    the same pmid, mark this one ``active``, and repoint ``documents.generation``.

    All within one transaction — a crash mid-switch rolls back and leaves the old
    generation ``active`` (the partial unique index guarantees at most one)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT pmid, status FROM generation_manifest WHERE generation=?",
            (generation,),
        ).fetchone()
        if row is None:
            raise GenerationError(f"generation {generation} does not exist")
        pmid, status = row
        if status not in ("building", "active"):
            raise GenerationError(f"cannot activate generation in status {status!r}")
        now = _now(conn)
        # retire the incumbent active generation for this pmid (if any, and not us)
        conn.execute(
            """UPDATE generation_manifest SET status='retired', retired_at=?
               WHERE pmid=? AND status='active' AND generation<>?""",
            (now, pmid, generation),
        )
        set_count = "" if chunk_count is None else ", chunk_count=?"
        params = [now]
        if chunk_count is not None:
            params.append(int(chunk_count))
        params.append(generation)
        conn.execute(
            f"""UPDATE generation_manifest
                SET status='active', activated_at=?{set_count}
                WHERE generation=?""",
            params,
        )
        conn.execute("UPDATE documents SET generation=? WHERE pmid=?", (generation, pmid))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def fail_generation(conn, generation: int, *, error: str) -> None:
    """Mark a building generation ``failed`` (rollback already discarded its rows)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE generation_manifest SET status='failed', error=? WHERE generation=?",
            (error, generation),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def cleanup_orphan_building(conn) -> list[int]:
    """Startup crash recovery: drop ``building`` generations that have no chunks
    (an interrupted build) and mark them ``failed``. Returns the affected ids."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """SELECT g.generation FROM generation_manifest g
               WHERE g.status='building'
                 AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.generation=g.generation)"""
        ).fetchall()
        orphans = [int(r[0]) for r in rows]
        for gen in orphans:
            conn.execute(
                "UPDATE generation_manifest SET status='failed', error='orphan_building_cleanup' WHERE generation=?",
                (gen,),
            )
        conn.execute("COMMIT")
        return orphans
    except Exception:
        conn.execute("ROLLBACK")
        raise


def active_generation_for(conn, pmid: str) -> Optional[int]:
    """The active generation for ``pmid`` (or None)."""
    row = conn.execute(
        "SELECT generation FROM generation_manifest WHERE pmid=? AND status='active'",
        (pmid,),
    ).fetchone()
    return int(row[0]) if row else None
