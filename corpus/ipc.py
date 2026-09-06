"""corpus.ipc — JSONL framing + idempotency cache (IMPLEMENTATION.md §5).

Wire protocol: one JSON object per line (``\\n``-terminated). ``schema_version`` is
``1``, ``request_id`` is globally unique, and a single frame is capped at 1 MiB.
The reader tolerates arbitrary half-packets — bytes are buffered and a frame is
only dispatched once a newline is seen — so a frame is never dispatched twice.

Idempotency: the worker records ``processing`` before doing work; a duplicate
request whose row is ``committed`` gets the *cached full response body* back (and
its request hash is checked); a ``processing`` row can only be taken over once its
lease (default 30 s) has expired; ``failed`` returns the cached error. A cache row
past ``expires_at`` is re-executed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024          # 1 MiB
DEFAULT_LEASE_SECONDS = 30
DEFAULT_TTL_SECONDS = 3600


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #

class IPCError(ValueError):
    """Base class for protocol/framing errors."""


class FrameTooLarge(IPCError):
    """A frame exceeded the 1 MiB limit."""


class InvalidFrame(IPCError):
    """A frame is not a valid schema_version=1 JSON object with a request_id."""


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_frame(obj: dict) -> bytes:
    """Serialize ``obj`` to a single newline-terminated JSON frame.

    Raises ``FrameTooLarge`` if the encoded line (excluding the newline) exceeds
    the 1 MiB cap."""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"frame_too_large: {len(payload)} > {MAX_FRAME_BYTES}")
    return payload + b"\n"


def write_frame(fp, obj: dict) -> None:
    """Write one frame to a binary file-like object."""
    fp.write(encode_frame(obj))


def parse_frame(line: str | bytes) -> dict:
    """Parse one frame line into a validated dict (schema_version=1 + request_id)."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise InvalidFrame(f"invalid_json: {e}") from e
    if not isinstance(obj, dict):
        raise InvalidFrame("frame must be a JSON object")
    if obj.get("schema_version") != SCHEMA_VERSION:
        raise InvalidFrame(f"unsupported schema_version: {obj.get('schema_version')!r}")
    if not obj.get("request_id"):
        raise InvalidFrame("missing request_id")
    return obj


class FrameReader:
    """Incremental JSONL reader tolerant of arbitrary half-packets.

    Feed it bytes as they arrive; ``feed`` returns the list of *complete* frames
    unpacked so far (possibly empty). Partial trailing bytes are retained until a
    newline arrives, guaranteeing each frame is dispatched exactly once."""

    def __init__(self, *, max_frame_bytes: int = MAX_FRAME_BYTES):
        self._buf = bytearray()
        self._max = max_frame_bytes

    def feed(self, data: bytes) -> list[dict]:
        self._buf.extend(data)
        frames: list[dict] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl == -1:
                # no complete frame yet; a partial larger than the cap is fatal
                if len(self._buf) > self._max:
                    raise FrameTooLarge(
                        f"frame_too_large: {len(self._buf)} > {self._max} (no newline)")
                break
            if nl > self._max:
                raise FrameTooLarge(f"frame_too_large: {nl} > {self._max}")
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if line.strip():
                frames.append(parse_frame(line))
        return frames

    @property
    def buffered(self) -> int:
        return len(self._buf)


# --------------------------------------------------------------------------- #
# idempotency cache
# --------------------------------------------------------------------------- #

IPC_IDEMPOTENCY_DDL = """
CREATE TABLE IF NOT EXISTS ipc_idempotency(
 idempotency_key TEXT PRIMARY KEY, request_id TEXT NOT NULL, command TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, response_json TEXT, response_sha256 TEXT,
 status TEXT NOT NULL CHECK(status IN ('processing','committed','failed')),
 error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
"""


def install_ipc_schema(conn) -> None:
    conn.executescript(IPC_IDEMPOTENCY_DDL)
    try:
        conn.commit()
    except Exception:
        pass


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def score_db_key(project: str, pmid: str, formula: str, rubric: str, score_event_id: str) -> str:
    """The database idempotency key for a score request — never includes scored_at."""
    return f"score:{project}:{pmid}:{formula}:{rubric}:{score_event_id}"


@dataclass
class Reservation:
    status: str              # 'new' | 'cached' | 'in_progress' | 'failed'
    response_json: Optional[str] = None
    error_json: Optional[str] = None
    request_conflict: bool = False


def reserve(conn, *, key: str, request_id: str, command: str, request_json: str,
            lease_seconds: int = DEFAULT_LEASE_SECONDS,
            ttl_seconds: int = DEFAULT_TTL_SECONDS,
            now: Optional[datetime] = None) -> Reservation:
    """Reserve/resolve an idempotency key.

    * no row / expired cache        → insert ``processing`` and return ``new``
    * ``committed`` within TTL      → return ``cached`` with the full response body
                                       (``request_conflict`` set if the request hash
                                       differs from the recorded one)
    * ``processing`` within lease   → return ``in_progress``
    * ``processing`` past lease      → take over (reset ``processing``) → ``new``
    * ``failed``                    → return ``failed`` with the cached error
    """
    now_dt = _now(now)
    now_s = _iso(now_dt)
    req_hash = sha256_hex(request_json)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT request_id, request_sha256, response_json, status, error_json, "
            "updated_at, expires_at FROM ipc_idempotency WHERE idempotency_key=?",
            (key,),
        ).fetchone()

        def _insert_processing():
            expires = _iso(now_dt + timedelta(seconds=ttl_seconds))
            conn.execute(
                "INSERT OR REPLACE INTO ipc_idempotency(idempotency_key, request_id, command, "
                "request_sha256, response_json, response_sha256, status, error_json, "
                "created_at, updated_at, expires_at) "
                "VALUES (?,?,?,?,NULL,NULL,'processing',NULL,?,?,?)",
                (key, request_id, command, req_hash, now_s, now_s, expires),
            )

        if row is None:
            _insert_processing()
            result = Reservation(status="new")
        else:
            (_rid, r_hash, resp_json, status, error_json, updated_at, expires_at) = row
            expired = now_s > expires_at
            if status == "committed" and not expired:
                result = Reservation(status="cached", response_json=resp_json,
                                     request_conflict=(r_hash != req_hash))
            elif status == "failed" and not expired:
                result = Reservation(status="failed", error_json=error_json)
            elif status == "processing" and not _lease_expired(updated_at, lease_seconds, now_dt):
                result = Reservation(status="in_progress")
            else:
                # expired cache, or a stale processing lease → take over / re-execute
                _insert_processing()
                result = Reservation(status="new")
        conn.execute("COMMIT")
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _lease_expired(updated_at: str, lease_seconds: int, now_dt: datetime) -> bool:
    try:
        base = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return now_dt >= base + timedelta(seconds=lease_seconds)


def commit_response(conn, *, key: str, response_json: str, now: Optional[datetime] = None) -> None:
    """Record the committed full response body for a key (idempotent replays read it)."""
    now_s = _iso(_now(now))
    conn.execute(
        "UPDATE ipc_idempotency SET status='committed', response_json=?, response_sha256=?, "
        "updated_at=? WHERE idempotency_key=?",
        (response_json, sha256_hex(response_json), now_s, key),
    )
    try:
        conn.commit()
    except Exception:
        pass


def mark_failed(conn, *, key: str, error_json: str, now: Optional[datetime] = None) -> None:
    now_s = _iso(_now(now))
    conn.execute(
        "UPDATE ipc_idempotency SET status='failed', error_json=?, updated_at=? "
        "WHERE idempotency_key=?",
        (error_json, now_s, key),
    )
    try:
        conn.commit()
    except Exception:
        pass
