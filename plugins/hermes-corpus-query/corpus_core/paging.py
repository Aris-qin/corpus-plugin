import base64
import hashlib
import hmac
import json
import os
import time

from .db import digest, encoded
from .errors import CorpusError, require
from .paths import bounded


def cursor_key(root):
    path = bounded(root / 'runtime' / 'cursor-key', root)
    path.parent.mkdir(exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(descriptor, 'wb') as stream:
        key = os.urandom(32)
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    return key


def validate_cursor(root, binding, cursor):
    key = cursor_key(root)
    try:
        require(type(cursor) is str and len(cursor) <= 4096, 'INVALID_CURSOR', 'invalid cursor size')
        raw, signature = cursor.split('.')
        require(hmac.compare_digest(hmac.new(key, raw.encode(), hashlib.sha256).hexdigest(), signature), 'INVALID_CURSOR', 'cursor signature mismatch')
        payload = json.loads(base64.urlsafe_b64decode(raw))
        require(payload['binding'] == digest(binding), 'CURSOR_STALE', 'cursor filters or snapshot changed')
        require(payload['expires'] > time.time(), 'CURSOR_EXPIRED', 'cursor expired')
        return payload
    except (ValueError, KeyError, TypeError) as error:
        raise CorpusError('INVALID_CURSOR', 'malformed cursor') from error


def page(root, rows, binding, limit, cursor=None):
    require(type(limit) is int and 1 <= limit <= 100, 'INVALID_LIMIT', 'limit must be integer 1..100')
    if cursor is None and len(rows) <= limit:
        return rows, None
    key = cursor_key(root)
    fingerprint = digest(rows)
    offset = 0
    if cursor:
        payload = validate_cursor(root, binding, cursor)
        require(payload['snapshot'] == fingerprint, 'CURSOR_STALE', 'cursor filters or snapshot changed')
        offset = payload['offset']
    next_cursor = None
    if offset + limit < len(rows):
        payload = {'binding': digest(binding), 'snapshot': fingerprint, 'expires': time.time() + 900, 'offset': offset + limit}
        raw = base64.urlsafe_b64encode(encoded(payload).encode()).decode()
        next_cursor = raw + '.' + hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()
    return rows[offset:offset + limit], next_cursor
