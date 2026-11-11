import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import CorpusError, require
from .paths import bounded


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def identity(prefix):
    return prefix + '_' + uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = '''
CREATE TABLE works(id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
 metadata TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 lifecycle TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle IN ('active','recycled','tombstone')),
 preferred_version TEXT REFERENCES source_versions(id), created_at TEXT NOT NULL);
CREATE TABLE blobs(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, path TEXT NOT NULL UNIQUE);
CREATE TABLE source_versions(id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
 version_kind TEXT NOT NULL, acquired_at TEXT NOT NULL, coverage TEXT NOT NULL,
 metadata TEXT NOT NULL, UNIQUE(work_id, id));
CREATE TABLE version_assets(version_id TEXT REFERENCES source_versions(id), hash TEXT REFERENCES blobs(hash),
 role TEXT NOT NULL, media_type TEXT NOT NULL, PRIMARY KEY(version_id,hash,role));
CREATE TABLE external_identifiers(owner_id TEXT REFERENCES works(id), scheme TEXT NOT NULL,
 raw_value TEXT NOT NULL, lookup_key TEXT NOT NULL, verification_source TEXT,
 PRIMARY KEY(owner_id,scheme,raw_value));
CREATE TABLE generations(id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES source_versions(id),
 pipeline TEXT NOT NULL, canonical TEXT NOT NULL, canonical_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('lexical_ready','complete')), warnings TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(version_id,pipeline));
CREATE TABLE active_generations(version_id TEXT PRIMARY KEY REFERENCES source_versions(id),
 generation_id TEXT NOT NULL UNIQUE REFERENCES generations(id));
CREATE TABLE chunks(id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES generations(id),
 ordinal INTEGER NOT NULL, text TEXT NOT NULL, locator TEXT NOT NULL, UNIQUE(generation_id,ordinal));
CREATE VIRTUAL TABLE chunk_fts USING fts5(chunk_id UNINDEXED,text);
CREATE TABLE embedding_profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL UNIQUE);
CREATE TABLE chunk_embeddings(chunk_id TEXT REFERENCES chunks(id), profile_id TEXT REFERENCES embedding_profiles(id),
 vector TEXT NOT NULL, PRIMARY KEY(chunk_id,profile_id));
CREATE TABLE contexts(id TEXT PRIMARY KEY, kind TEXT NOT NULL, aliases TEXT NOT NULL);
CREATE TABLE context_links(context_id TEXT REFERENCES contexts(id), target_id TEXT NOT NULL,
 PRIMARY KEY(context_id,target_id));
CREATE TABLE research_records(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
 current_revision_id TEXT REFERENCES research_revisions(id), revision INTEGER NOT NULL DEFAULT 0,
 project_context TEXT REFERENCES contexts(id), journal_id TEXT REFERENCES journals(id));
CREATE UNIQUE INDEX one_overview ON research_records(project_context) WHERE kind='project_overview';
CREATE TABLE research_revisions(id TEXT PRIMARY KEY, record_id TEXT REFERENCES research_records(id),
 parent_id TEXT REFERENCES research_revisions(id), body TEXT NOT NULL, hash TEXT NOT NULL,
 evidence TEXT NOT NULL, structured TEXT NOT NULL, change_summary TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE citations(id TEXT PRIMARY KEY, from_revision_id TEXT REFERENCES research_revisions(id),
 target_version_id TEXT REFERENCES source_versions(id), target_revision_id TEXT REFERENCES research_revisions(id),
 chunk_id TEXT REFERENCES chunks(id), locator TEXT NOT NULL);
CREATE TABLE journals(id TEXT PRIMARY KEY, name TEXT NOT NULL, identifiers TEXT NOT NULL);
CREATE TABLE review_events(id TEXT PRIMARY KEY, target TEXT NOT NULL, scope TEXT NOT NULL,
 source TEXT NOT NULL, checked_at TEXT NOT NULL, conclusion TEXT NOT NULL, remaining TEXT NOT NULL);
CREATE TABLE external_links(id TEXT PRIMARY KEY, target TEXT NOT NULL, uri TEXT NOT NULL,
 basis TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(target,uri));
CREATE TABLE impact_notices(id TEXT PRIMARY KEY, link_id TEXT REFERENCES external_links(id),
 reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL,
 checkpoint TEXT NOT NULL DEFAULT '{}', error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 budget TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE job_steps(job_id TEXT REFERENCES jobs(id), attempt INTEGER NOT NULL,
 state TEXT NOT NULL, details TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(job_id,attempt));
CREATE TABLE proposals(id TEXT PRIMARY KEY, payload TEXT NOT NULL, hash TEXT NOT NULL,
 state TEXT NOT NULL, expires_at REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE approvals(id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE REFERENCES proposals(id),
 payload_hash TEXT NOT NULL, binding TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
CREATE TABLE batch_items(proposal_id TEXT REFERENCES proposals(id), ordinal INTEGER NOT NULL,
 state TEXT NOT NULL, result TEXT, error TEXT, PRIMARY KEY(proposal_id,ordinal));
CREATE TABLE receipts(operation_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE audit(id INTEGER PRIMARY KEY, event TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE export_outbox(id TEXT PRIMARY KEY, revision_id TEXT REFERENCES research_revisions(id),
 state TEXT NOT NULL DEFAULT 'pending', error TEXT);
CREATE TABLE legacy_map(source_hash TEXT NOT NULL, table_name TEXT NOT NULL, legacy_id TEXT NOT NULL,
 raw_payload TEXT NOT NULL, target_id TEXT, PRIMARY KEY(source_hash,table_name,legacy_id));
CREATE TABLE migration_issues(id INTEGER PRIMARY KEY, source_hash TEXT, legacy_id TEXT, reason TEXT NOT NULL);
CREATE TABLE migrations(source_hash TEXT PRIMARY KEY, evidence TEXT NOT NULL);
PRAGMA user_version=4;
'''


def connect(root):
    path = bounded(root / 'catalogue.sqlite', root)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=5000')
    connection.execute('PRAGMA journal_mode=WAL')
    try:
        connection.execute('BEGIN IMMEDIATE')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        require(version in (0, 4), 'SCHEMA_UNSUPPORTED', 'unknown candidate schema')
        if version == 0:
            require(not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                    'SCHEMA_UNSUPPORTED', 'refuse to initialize an existing unknown database')
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    connection.execute(statement)
            for table in ('source_versions', 'blobs', 'version_assets', 'generations', 'chunks',
                          'research_revisions', 'citations', 'audit', 'review_events', 'legacy_map'):
                for action in ('UPDATE', 'DELETE'):
                    connection.execute(f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable content'); END")
            ownership = [
                ('active_generations', 'generation_id', 'generations', 'version_id', 'version_id'),
                ('works', 'preferred_version', 'source_versions', 'work_id', 'id'),
                ('research_records', 'current_revision_id', 'research_revisions', 'record_id', 'id'),
            ]
            for table, pointer, targets, owner, source in ownership:
                for action in ('INSERT', 'UPDATE'):
                    connection.execute(f"CREATE TRIGGER owner_{table}_{action} BEFORE {action} ON {table} WHEN NEW.{pointer} IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {targets} WHERE id=NEW.{pointer} AND {owner}=NEW.{source}) BEGIN SELECT RAISE(ABORT,'cross-owner pointer'); END")
            connection.execute("CREATE TRIGGER frozen_proposal BEFORE UPDATE OF payload,hash,expires_at ON proposals BEGIN SELECT RAISE(ABORT,'immutable proposal payload'); END")
            connection.execute("CREATE TRIGGER frozen_approval BEFORE UPDATE OF proposal_id,payload_hash,binding,at ON approvals BEGIN SELECT RAISE(ABORT,'immutable approval binding'); END")
        connection.execute('CREATE TABLE IF NOT EXISTS legacy_vectors(source_hash TEXT,legacy_chunk_id TEXT,chunk_id TEXT REFERENCES chunks(id),profile TEXT NOT NULL,raw_vector TEXT NOT NULL,PRIMARY KEY(source_hash,legacy_chunk_id))')
        connection.execute('CREATE TABLE IF NOT EXISTS legacy_relationships(source_hash TEXT,table_name TEXT,legacy_id TEXT,field TEXT,target_ref TEXT,related_ref TEXT,raw_value TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS embedding_batches(id TEXT PRIMARY KEY,policy_id TEXT NOT NULL,profile_id TEXT NOT NULL,state TEXT NOT NULL,input_chars INTEGER NOT NULL,vectors TEXT,error TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS job_parse_cache(job_id TEXT PRIMARY KEY REFERENCES jobs(id),pipeline TEXT NOT NULL,result TEXT NOT NULL)')
        connection.execute("CREATE TABLE IF NOT EXISTS embedding_requests(batch_id TEXT PRIMARY KEY REFERENCES embedding_batches(id),input_count INTEGER NOT NULL,request_key TEXT NOT NULL)")
        connection.execute('CREATE TABLE IF NOT EXISTS legacy_adoptions(source_hash TEXT NOT NULL,legacy_id TEXT NOT NULL,manifest_hash TEXT NOT NULL,blob_hash TEXT NOT NULL REFERENCES blobs(hash),PRIMARY KEY(source_hash,legacy_id))')
        incomplete = [row['id'] for row in connection.execute("SELECT id FROM jobs WHERE state='succeeded' AND json_extract(checkpoint,'$.vector_status') IS NOT NULL AND json_extract(checkpoint,'$.vector_status') NOT IN ('complete','not_configured')")]
        for job_id in incomplete:
            connection.execute("UPDATE jobs SET state='partial' WHERE id=?", (job_id,))
            audit(connection, 'incomplete_index_state_repaired', {'job_id': job_id, 'historical_steps_preserved': True})
        connection.execute("CREATE TABLE IF NOT EXISTS purge_journal(operation_id TEXT PRIMARY KEY,target TEXT NOT NULL UNIQUE REFERENCES works(id),manifest TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('cleanup_pending','purged')),created_at TEXT NOT NULL)")
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        connection.close()
        raise
    return connection


@contextmanager
def transaction(connection):
    try:
        connection.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as error:
        if getattr(error, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise CorpusError('DB_BUSY', 'candidate database is busy', retryable=True) from error
        raise
    try:
        yield
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise


def audit(connection, event, payload):
    connection.execute('INSERT INTO audit(event,payload,at) VALUES (?,?,?)', (event, encoded(payload), now()))
