import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from .db import audit, digest, encoded, identity, now, transaction
from .errors import CorpusError, require
from .objects import publish
from .paths import bounded


def raw_value(value):
    if isinstance(value, bytes):
        return {'$bytes': base64.b64encode(value).decode()}
    if type(value) is float:
        return {'$float_hex': value.hex()}
    return value


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(corpus, source_path):
    source_path = bounded(source_path, corpus.sandbox_root)
    require(source_path.exists() and source_path != corpus.root / 'catalogue.sqlite', 'INVALID_SOURCE', 'explicit distinct legacy source required')
    directory = bounded(corpus.root / 'backups', corpus.root)
    directory.mkdir(exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, suffix='.sqlite')
    os.close(descriptor)
    try:
        with sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True) as source:
            source.execute('PRAGMA query_only=ON')
            with sqlite3.connect(temporary) as target:
                source.backup(target, pages=128, sleep=0.01)
        data = bounded(temporary, corpus.root).read_bytes()
        blob = publish(corpus.root, data)
        return corpus.root / blob['path'], blob['hash']
    finally:
        os.unlink(temporary)


def inspect_snapshot(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        require(version in (0, 1, 2, 3), 'SCHEMA_UNSUPPORTED', 'unknown legacy schema version')
        schema = [dict(row) for row in connection.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")]
        virtual = [row for row in schema if 'CREATE VIRTUAL TABLE' in row['sql'].upper()]
        for row in virtual:
            require(row['name'] == 'chunk_vectors' and re.search(r'\bUSING\s+vec0\s*\(', row['sql'], re.I)
                    and re.search(r'embedding\s+float\s*\[1024\]', row['sql'], re.I),
                    'SCHEMA_UNSUPPORTED', 'unknown virtual module or future vector DDL')
        if virtual:
            try:
                import sqlite_vec
            except ImportError as error:
                raise CorpusError('LEGACY_EXTENSION_UNAVAILABLE', 'optional trusted sqlite_vec package required; backup retained') from error
            require(Path(sqlite_vec.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),
                    'LEGACY_EXTENSION_UNAVAILABLE', 'sqlite_vec must come from trusted installed environment')
            try:
                connection.enable_load_extension(True)
                sqlite_vec.load(connection)
            finally:
                connection.enable_load_extension(False)
        tables = [row['name'] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') ORDER BY name")]
        require({'documents', 'chunks', 'document_groups'} <= set(tables), 'SCHEMA_UNSUPPORTED', 'unknown legacy schema: required tables missing')
        required = {'documents': {'pmid', 'title'}, 'chunks': {'chunk_id', 'pmid', 'text'}, 'document_groups': {'pmid'}}
        for table, columns in required.items():
            actual = {row['name'] for row in connection.execute('PRAGMA table_info(' + quoted(table) + ')')}
            require(columns <= actual, 'SCHEMA_UNSUPPORTED', 'legacy schema columns not supported')
        known_tables = {'documents', 'chunks', 'document_groups', 'projects', 'project_queries', 'relevance', 'quality_evidence', 'venue', 'sqlite_sequence'}
        vector_tables = {'chunk_vectors', 'chunk_vectors_info', 'chunk_vectors_chunks', 'chunk_vectors_rowids', 'chunk_vectors_vector_chunks00'} if virtual else set()
        require(set(tables) <= known_tables | vector_tables and not any(row['type'] in ('view', 'trigger') for row in schema),
                'SCHEMA_UNSUPPORTED', 'unknown future legacy DDL; backup retained')
        known_columns = {
            'documents': 'pmid title doi source retrieved_at has_fulltext raw_path article_type quality_type_prior abstract_excerpt created_at extra',
            'chunks': 'chunk_id pmid ordinal heading_path section_importance text snippet snippet_source char_start char_end created_at level path parent_id child_ids sibling_ids heading_chain doc_id embedding',
            'document_groups': 'pmid project_slug role added_at notes project project_id group_id extra',
            'projects': 'slug title status description created_at updated_at',
            'project_queries': 'id project_slug query pmid_count executed_at',
            'relevance': 'chunk_id query chunk_relevance heading_match jaccard computed_at',
            'quality_evidence': 'pmid criterion_validity outcome_reliability conclusion_data_consistency evidence_mean quality_final computed_at notes',
            'venue': 'pmid journal year impact_factor jcr_quartile cas_zone source',
            'sqlite_sequence': 'name seq', 'chunk_vectors': 'chunk_id embedding',
            'chunk_vectors_info': 'key value', 'chunk_vectors_chunks': 'chunk_id size validity rowids',
            'chunk_vectors_rowids': 'rowid id chunk_id chunk_offset', 'chunk_vectors_vector_chunks00': 'rowid vectors',
        }
        for table in tables:
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(' + quoted(table) + ')')}
            require(columns <= set(known_columns[table].split()), 'SCHEMA_UNSUPPORTED', 'unknown future legacy column DDL; backup retained')
        data = {}
        counts = {}
        for table in tables:
            count = connection.execute('SELECT count(*) FROM ' + quoted(table)).fetchone()[0]
            require(count <= 100000, 'MIGRATION_LIMIT', 'parent must authorize larger snapshot')
            rows = [dict(row) for row in connection.execute('SELECT * FROM ' + quoted(table))]
            data[table] = rows
            counts[table] = count
            require(len(rows) == count, 'MIGRATION_VERIFY', 'source count differs')
        return schema, data, counts
    finally:
        connection.close()


def preserved_payload(row):
    return {field: raw_value(value) for field, value in row.items()}


def migrate(corpus, source_path):
    path, source_hash = snapshot(corpus, source_path)
    previous = corpus.db.execute('SELECT evidence FROM migrations WHERE source_hash=?', (source_hash,)).fetchone()
    if previous:
        return json.loads(previous['evidence'])
    require(corpus.db.execute('SELECT 1 FROM migrations LIMIT 1').fetchone() is None,
            'MIGRATION_LINEAGE_CHANGED', 'changed or different snapshot cannot import into a nonempty migration candidate; use a fresh candidate')
    schema, data, counts = inspect_snapshot(path)
    canonical = {table: sorted([encoded(preserved_payload(row)) for row in rows]) for table, rows in data.items()}
    source_payload_hash = digest(canonical)
    with transaction(corpus.db):
        previous = corpus.db.execute('SELECT evidence FROM migrations WHERE source_hash=?', (source_hash,)).fetchone()
        if previous:
            return json.loads(previous['evidence'])
        require(corpus.db.execute('SELECT 1 FROM migrations LIMIT 1').fetchone() is None,
                'MIGRATION_LINEAGE_CHANGED', 'changed snapshot requires a fresh migration candidate')
        mapping = {}
        versions = {}
        for document in data['documents']:
            old_id = str(document['pmid'])
            require(old_id not in mapping, 'SCHEMA_UNSUPPORTED', 'duplicate legacy document primary key')
            work_id, version_id = identity('work'), identity('version')
            mapping[old_id], versions[old_id] = work_id, version_id
            metadata = {'classification': 'unresolved', 'source': document.get('source'),
                        'legacy_raw_path': document.get('raw_path'), 'custody': 'external_unverified',
                        'approval': 'no_prior_approval_inferred', 'embedding_profile': 'legacy_unknown'}
            corpus.db.execute('INSERT INTO works(id,kind,title,metadata,created_at) VALUES (?,?,?,?,?)',
                              (work_id, 'other', str(document.get('title') or ''), encoded(metadata), now()))
            coverage = 'unknown'
            corpus.db.execute('INSERT INTO source_versions VALUES (?,?,?,?,?,?)',
                              (version_id, work_id, 'legacy_unverified', now(), coverage, encoded({'legacy_claim_only': True, 'original_acquired_at': document.get('retrieved_at')})))
            corpus.db.execute('UPDATE works SET preferred_version=? WHERE id=?', (version_id, work_id))
            for reason in ('source_classification_unresolved', 'legacy_embedding_model_unknown', 'raw_path_external_unverified'):
                corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (source_hash, old_id, reason))
        chunk_targets = {}
        for old_id, version_id in versions.items():
            legacy_chunks = [row for row in data['chunks'] if str(row['pmid']) == old_id]
            if not legacy_chunks:
                continue
            text = '\n'.join(str(row.get('text') or '') for row in legacy_chunks)
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            generation = identity('generation')
            corpus.db.execute('INSERT INTO generations VALUES (?,?,?,?,?,?,?,?)',
                              (generation, version_id, 'legacy-preserved-' + source_hash, text, text_hash, 'lexical_ready',
                              encoded(['original_not_in_custody', 'legacy_locators_unverified', 'legacy_vector_profile_unknown', 'source_classification_unresolved']), now()))
            for ordinal, chunk in enumerate(legacy_chunks):
                chunk_id = identity('chunk')
                old_chunk_id = str(chunk['chunk_id'])
                require(old_chunk_id not in chunk_targets, 'SCHEMA_UNSUPPORTED', 'duplicate legacy chunk id')
                chunk_targets[old_chunk_id] = chunk_id
                locator = {'basis': 'legacy_unverified', 'page': None,
                           'char_start': chunk.get('char_start'), 'char_end': chunk.get('char_end'),
                           'heading_path': chunk.get('heading_path'), 'canonical_hash': text_hash}
                corpus.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)', (chunk_id, generation, ordinal, str(chunk.get('text') or ''), encoded(locator)))
                corpus.db.execute('INSERT INTO chunk_fts VALUES (?,?)', (chunk_id, str(chunk.get('text') or '')))
            original = next(row for row in data['documents'] if str(row['pmid']) == old_id)
            if original.get('source') in {'pubmed', 'pmc', 'file_parse', 'url_fetch', 'glm_web'}:
                corpus.db.execute('INSERT INTO active_generations VALUES (?,?)', (version_id, generation))
            else:
                corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (source_hash, old_id, 'unresolved_self_written_or_unknown_quarantined_from_search'))
        for project in data.get('projects', []):
            slug = project.get('slug')
            if slug:
                corpus.db.execute('INSERT OR IGNORE INTO contexts VALUES (?,?,?)', (str(slug), 'project', encoded(preserved_payload(project))))
            else:
                corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (source_hash, None, 'project_without_stable_slug'))
        for relation in data['document_groups']:
            target = mapping.get(str(relation['pmid']))
            project = relation.get('project_slug') or relation.get('project') or relation.get('project_id') or relation.get('group_id')
            if target and project is not None:
                corpus._link(target, str(project))
            else:
                corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (source_hash, str(relation['pmid']), 'relation_preserved_but_unresolved'))
        for vector in data.get('chunk_vectors', []):
            old_chunk = str(vector['chunk_id'])
            corpus.db.execute('INSERT INTO legacy_vectors VALUES (?,?,?,?,?)',
                (source_hash, old_chunk, chunk_targets.get(old_chunk), 'legacy_unknown', encoded(raw_value(vector['embedding']))))
            if old_chunk not in chunk_targets:
                corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (source_hash, old_chunk, 'orphan_vector_preserved'))
        for table, rows in data.items():
            for ordinal, row in enumerate(rows):
                if table == 'documents':
                    legacy_id, target_id = str(row['pmid']), 'corpus://work/' + mapping[str(row['pmid'])]
                elif table == 'chunks':
                    legacy_id = str(row['chunk_id'])
                    target_id = 'corpus://chunk/' + chunk_targets[legacy_id] if legacy_id in chunk_targets else None
                    if target_id is None:
                        corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (source_hash, legacy_id, 'orphan_chunk_preserved'))
                else:
                    legacy_id, target_id = str(ordinal), None
                    if table in ('relevance', 'chunk_vectors') and str(row.get('chunk_id')) in chunk_targets:
                        target_id = 'corpus://chunk/' + chunk_targets[str(row['chunk_id'])]
                    elif table in ('quality_evidence', 'venue', 'document_groups') and str(row.get('pmid')) in mapping:
                        target_id = 'corpus://work/' + mapping[str(row['pmid'])]
                corpus.db.execute('INSERT INTO legacy_map VALUES (?,?,?,?,?)',
                                  (source_hash, table, legacy_id, encoded(preserved_payload(row)), target_id))
                if table not in {'documents', 'sqlite_sequence'} and not table.startswith('chunk_vectors_'):
                    for field in ('pmid', 'chunk_id', 'project_slug', 'parent_id', 'child_ids', 'sibling_ids', 'doc_id'):
                        if row.get(field) is None:
                            continue
                        values = row[field]
                        if field in ('child_ids', 'sibling_ids'):
                            try:
                                values = json.loads(values)
                            except (ValueError, TypeError):
                                values = [values]
                        if type(values) is not list:
                            values = [values]
                        for value in values:
                            if field == 'project_slug':
                                context = corpus.db.execute('SELECT id FROM contexts WHERE id=?', (str(value),)).fetchone()
                                related = 'context:' + str(value) if context else None
                            elif field in ('chunk_id', 'parent_id', 'child_ids', 'sibling_ids'):
                                related = 'corpus://chunk/' + chunk_targets[str(value)] if str(value) in chunk_targets else None
                            else:
                                related = 'corpus://work/' + mapping[str(value)] if str(value) in mapping else None
                            corpus.db.execute('INSERT INTO legacy_relationships VALUES (?,?,?,?,?,?,?)',
                                (source_hash, table, legacy_id, field, target_id, related, encoded(raw_value(value))))
                            if related is None:
                                corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (source_hash, legacy_id, table + '.' + field + '_relationship_unresolved'))
        restored = {}
        verified_counts = {}
        for table in data:
            rows = corpus.db.execute('SELECT raw_payload FROM legacy_map WHERE source_hash=? AND table_name=?', (source_hash, table)).fetchall()
            restored[table] = sorted(row['raw_payload'] for row in rows)
            verified_counts[table] = len(rows)
        require(verified_counts == counts and digest(restored) == source_payload_hash, 'MIGRATION_VERIFY', 'independent mapping counts/hash mismatch')
        evidence = {'source_hash': source_hash, 'snapshot_object': str(path.relative_to(corpus.root)),
                    'counts': counts, 'verified_counts': verified_counts, 'raw_payload_hash': source_payload_hash,
                    'mapped_payload_hash': digest(restored), 'schema': schema, 'verified': True,
                    'source_open_mode': 'ro_query_only_backup_api_wal', 'raw_files': 'not_read_external_unverified',
                    'vectors': 'raw_values_preserved_not_reused', 'production_snapshot': False}
        corpus.db.execute('INSERT INTO migrations VALUES (?,?)', (source_hash, encoded(evidence)))
        audit(corpus.db, 'migration_verified', evidence)
        return evidence


class LegacyResolver:
    def resolve_legacy(self, old_id):
        rows = self.db.execute('SELECT * FROM legacy_map WHERE legacy_id=? ORDER BY source_hash,table_name LIMIT 101', (old_id,)).fetchall()
        require(bool(rows), 'NOT_FOUND', 'legacy id not mapped')
        require(len(rows) <= 100, 'AMBIGUOUS_LEGACY_ID', 'legacy id matches too many mappings; use explicit stable URI')
        matches = []
        for row in rows:
            match = dict(row)
            match['resolved'] = self.read(row['target_id']) if row['target_id'] else None
            matches.append(match)
        return {'matches': matches, 'ambiguous': len(matches) > 1, 'old_id': old_id}
