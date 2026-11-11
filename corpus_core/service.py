import hashlib
import json
import time
from urllib.parse import urlsplit

from .db import audit, connect, digest, encoded, identity, now, transaction
from .errors import CorpusError, require
from .objects import publish, verified_blob
from .parsing import parse
from .paths import bounded, data_root, sandbox_root as authorized_sandbox_root
from .retrieval import Retrieval
from .research import Research
from .management import Management
from .migration import LegacyResolver
from .identity import identifier_conflict, normalize_identifier
from .purge import Purge
from .adoption import Adoption


class Corpus(Retrieval, Research, Management, LegacyResolver, Purge, Adoption):
    def __init__(self, root, test_mode=False, sandbox_root=None, acquisition=None, safety_mode='disabled', legacy_verifier=None):
        require(safety_mode == 'disabled', 'INVALID_SETTINGS', 'no enable mode is shipped without a verifiable human approval adapter')
        self.safety_mode = safety_mode
        require(legacy_verifier is None or callable(legacy_verifier), 'INVALID_SETTINGS', 'legacy verifier must be injected by trusted local integration code')
        self.legacy_verifier = legacy_verifier
        self.sandbox_root = authorized_sandbox_root(sandbox_root)
        self.root = data_root(root, self.sandbox_root)
        self.test_mode = test_mode
        require(acquisition is None or test_mode, 'TEST_ADAPTER_FORBIDDEN', 'acquisition fixtures require explicit test mode')
        self.acquisition = acquisition
        self.embedding = None
        self.rerank = None
        self.db = connect(self.root)

    def close(self):
        self.db.close()

    def require_sensitive_actions(self):
        require(False, 'SENSITIVE_ACTION_DISABLED',
                'SENSITIVE_ACTION_DISABLED: no verifiable host human approval adapter; publication, adoption batches, merge and permanent deletion disabled')

    @classmethod
    def from_settings(cls, path, sandbox_root=None, test_mode=False, embedding_transport=None, rerank_transport=None):
        from .settings import EmbeddingRuntime, load_settings
        settings = load_settings(path, sandbox_root)
        corpus = cls(settings['data_root'], test_mode=test_mode, sandbox_root=sandbox_root)
        try:
            if settings['embedding'] is not None:
                corpus.embedding = EmbeddingRuntime(corpus, settings['embedding'], embedding_transport)
            require(rerank_transport is None or test_mode, 'TEST_ADAPTER_FORBIDDEN', 'rerank fixtures require explicit test mode')
            if settings.get('rerank') is not None:
                from .rerank import RerankRuntime
                corpus.rerank = RerankRuntime(corpus, settings['rerank'], rerank_transport)
            return corpus
        except BaseException:
            corpus.close()
            raise

    def receipt(self, operation_id, payload):
        require(type(operation_id) is str and 0 < len(operation_id) <= 200,
                'INVALID_OPERATION', 'operation_id required, at most 200 characters')
        row = self.db.execute('SELECT * FROM receipts WHERE operation_id=?', (operation_id,)).fetchone()
        if row:
            require(row['input_hash'] == digest(payload), 'IDEMPOTENCY_CONFLICT', 'same operation_id with different input')
            return json.loads(row['result'])

    def mutate(self, operation_id, payload, action):
        with transaction(self.db):
            previous = self.receipt(operation_id, payload)
            if previous is not None:
                return previous
            result = action()
            self.db.execute('INSERT INTO receipts VALUES (?,?,?)', (operation_id, digest(payload), encoded(result)))
            audit(self.db, 'mutation', {'operation_id': operation_id, 'input_hash': digest(payload)})
            return result

    def preflight(self, inputs):
        from .contracts import INPUTS, validate_value
        validate_value(inputs, INPUTS, 'inputs')
        require(type(inputs) is list and 0 < len(inputs) <= 10, 'NEEDS_SCOPE_APPROVAL', 'normal batch is 1..10 inputs')
        total = 0
        for item in inputs:
            require(type(item) is dict, 'INVALID_INPUT', 'input must be an object')
            require(set(item) <= {'source', 'content', 'path', 'url', 'identifier', 'title', 'media_type', 'contexts', 'coverage', 'kind',
                                 'work_id', 'expected_revision', 'version_kind', 'relationship_source', 'identifiers'},
                    'INVALID_INPUT', 'unknown input field')
            require(item.get('source') in ('text', 'file', 'url', 'identifier'), 'INVALID_INPUT', 'unknown source')
            require(type(item.get('title')) is str and 0 < len(item['title']) <= 1000, 'INVALID_INPUT', 'title required')
            require(item.get('coverage', 'unknown') in ('abstract', 'fulltext', 'unknown'), 'INVALID_INPUT', 'invalid coverage')
            require(item.get('kind', 'other') in ('paper', 'web', 'guideline', 'template', 'other'), 'INVALID_INPUT', 'invalid work kind')
            require(item.get('version_kind', 'snapshot') in ('preprint', 'formal', 'snapshot', 'correction', 'supplement'), 'INVALID_INPUT', 'invalid version kind')
            if item.get('work_id'):
                require(type(item.get('expected_revision')) is int and item.get('relationship_source'), 'INVALID_VERSION', 'existing work requires expected revision and reliable relationship source')
                self.evidence_target(item['relationship_source'])
            else:
                require(not (set(item) & {'expected_revision', 'relationship_source'}), 'INVALID_VERSION', 'relationship fields require work_id')
            for identifier in item.get('identifiers', []):
                require(type(identifier) is dict and set(identifier) == {'scheme', 'raw_value', 'verification_source'}, 'INVALID_IDENTIFIER', 'strict identifier fields required')
                normalize_identifier(identifier['scheme'], identifier['raw_value'])
            require(item.get('media_type', 'text/plain') in ('text/plain', 'text/markdown', 'text/html', 'application/pdf'),
                    'INVALID_INPUT', 'unsupported media type')
            require(type(item.get('contexts', [])) is list and all(type(context) is str and 0 < len(context) <= 200 for context in item.get('contexts', [])),
                    'INVALID_INPUT', 'contexts must be explicit identifiers')
            if item['source'] == 'text':
                require(type(item.get('content')) is str and not (set(item) & {'path', 'url', 'identifier'}),
                        'INVALID_INPUT', 'text requires only content')
                size = len(item['content'].encode())
            elif item['source'] == 'file':
                require(type(item.get('path')) is str and not (set(item) & {'content', 'url', 'identifier'}),
                        'INVALID_INPUT', 'file requires only path')
                size = bounded(item['path'], self.root).stat().st_size
            else:
                field = 'url' if item['source'] == 'url' else 'identifier'
                require(type(item.get(field)) is str and not (set(item) & ({'content', 'path', 'url', 'identifier'} - {field})), 'INVALID_INPUT', 'acquisition source fields mismatch')
                self.acquisition_url(item)
                size = 10 * 1024 * 1024
            require(size <= 10 * 1024 * 1024, 'NEEDS_SCOPE_APPROVAL', 'per-input parser limit 10 MiB')
            total += size
        require(total <= 100 * 1024 * 1024, 'NEEDS_SCOPE_APPROVAL', 'batch over 100 MiB')
        return {'bytes': total, 'inputs': len(inputs), 'cost': self.embedding.policy['max_cost_usd'] if self.embedding else 0,
                'cost_basis': 'operator_supplied_policy_ceiling_not_verified_provider_invoice' if self.embedding else 'offline_lexical',
                'network': self.embedding is not None or any(item['source'] in ('url', 'identifier') for item in inputs), 'index': 'configured_embedding_bounded' if self.embedding else 'lexical'}

    def acquisition_url(self, item):
        if item['source'] == 'identifier':
            from urllib.parse import quote
            return 'https://doi.org/' + quote(normalize_identifier('doi', item['identifier']), safe='/')
        url = normalize_identifier('url', item['url'])
        parts = urlsplit(url)
        require(parts.scheme == 'https' and parts.port in (None, 443), 'UNSAFE_URL', 'only HTTPS port 443 acquisition supported')
        return url

    def acquire(self, item):
        from .network import fetch_snapshot
        import os
        url = self.acquisition_url(item)
        require(self.acquisition is not None or os.environ.get('CORPUS_OFFLINE') != '1', 'NETWORK_DISABLED', 'offline profile forbids acquisition including DNS')
        acquired = (self.acquisition or fetch_snapshot)(url)
        content = acquired['content']
        require(type(content) is bytes and 0 < len(content) <= 10 * 1024 * 1024, 'RESPONSE_LIMIT', 'acquired bytes outside bounds')
        media = acquired['media_type']
        require(media in ('text/plain', 'text/markdown', 'text/html', 'application/pdf'), 'ACQUISITION_TYPE', 'unsupported acquired content type')
        if media == 'application/pdf':
            require(content.startswith(b'%PDF-'), 'ACQUISITION_TYPE', 'response is not a PDF')
        else:
            lowered = content[:200000].lower()
            require(not any(marker in lowered for marker in (b'type="password"', b"type='password'", b'sign in to access', b'log in to access', b'access denied', b'captcha')),
                    'ACQUISITION_ACCESS_REQUIRED', 'login/challenge response is not source evidence')
        provenance = {key: acquired[key] for key in ('url', 'media_type', 'redirects', 'dns_pinned')}
        provenance.update({'requested_url': url, 'original_hash': hashlib.sha256(content).hexdigest(), 'acquired_at': now(), 'coverage_claim': 'snapshot_not_verified_fulltext'})
        identifiers = list(item.get('identifiers', []))
        if item['source'] == 'identifier':
            identifiers.append({'scheme': 'doi', 'raw_value': item['identifier'],
                                'verification_source': 'deterministic_resolver_snapshot_not_bibliographic_verification'})
        return {**item, 'identifiers': identifiers, 'media_type': media, 'coverage': 'unknown', 'acquisition': provenance}, content

    def ingest(self, payload):
        previous = self.receipt(payload.get('operation_id'), {'tool': 'ingest', **payload})
        if previous is not None:
            return previous
        estimate = self.preflight(payload['inputs'])
        prepared = []
        for item in payload['inputs']:
            if item['source'] in ('url', 'identifier'):
                item, content = self.acquire(item)
            else:
                content = item['content'].encode() if item['source'] == 'text' else bounded(item['path'], self.root).read_bytes()
            require(len(content) <= 10 * 1024 * 1024, 'NEEDS_SCOPE_APPROVAL', 'input grew beyond approved bound')
            prepared.append((item, content))

        def save():
            items = []
            for item, content in prepared:
                content_hash = hashlib.sha256(content).hexdigest()
                require(self.db.execute("SELECT 1 FROM purge_journal j,json_each(j.manifest) b WHERE j.state='cleanup_pending' AND json_extract(b.value,'$.hash')=?", (content_hash,)).fetchone() is None,
                        'PURGE_PENDING', 'owned bytes have pending deletion; retry purge before ingest')
                blob = publish(self.root, content)
                if item.get('work_id'):
                    work = self.db.execute('SELECT * FROM works WHERE id=?', (item['work_id'],)).fetchone()
                    require(work is not None and work['revision'] == item['expected_revision'] and work['lifecycle'] == 'active', 'REVISION_CONFLICT', 'work revision/lifecycle changed')
                existing = self.db.execute("SELECT v.id,v.work_id FROM source_versions v JOIN version_assets a ON a.version_id=v.id WHERE a.hash=? AND v.version_kind=? AND v.coverage=? AND a.media_type=? AND json_extract(v.metadata,'$.source')=? AND coalesce(json_extract(v.metadata,'$.acquisition.requested_url'),'')=? AND coalesce(json_extract(v.metadata,'$.relationship_source'),'')=? LIMIT 1",
                    (blob['hash'], item.get('version_kind', 'snapshot'), item.get('coverage', 'unknown'), item.get('media_type', 'text/plain'), item['source'], (item.get('acquisition') or {}).get('requested_url', ''), item.get('relationship_source', ''))).fetchone()
                same_blob = self.db.execute('SELECT v.work_id FROM source_versions v JOIN version_assets a ON a.version_id=v.id WHERE a.hash=? LIMIT 1', (blob['hash'],)).fetchone()
                require(not same_blob or not item.get('work_id') or same_blob['work_id'] == item['work_id'], 'IDENTITY_REVIEW_REQUIRED', 'same bytes across works require identity review')
                if existing:
                    work_id, version_id = existing['work_id'], existing['id']
                    existing_work = self.db.execute('SELECT lifecycle FROM works WHERE id=?', (work_id,)).fetchone()
                    require(existing_work['lifecycle'] == 'active', 'REVISION_CONFLICT', 'deduplicated work is not active; restore/review explicitly')
                    require(not item.get('work_id') or item['work_id'] == work_id, 'IDENTITY_REVIEW_REQUIRED', 'same bytes under different work requires explicit identity review')
                    job = self.db.execute('SELECT id FROM jobs WHERE json_extract(payload,\'$.version_id\')=? ORDER BY created_at LIMIT 1', (version_id,)).fetchone()
                    job_id = job['id'] if job else None
                else:
                    work_id, version_id, job_id = item.get('work_id') or identity('work'), identity('version'), identity('job')
                    require(not same_blob or item.get('work_id'), 'INVALID_VERSION', 'same bytes with different semantics require explicit work revision')
                    if item.get('work_id'):
                        work = self.db.execute('SELECT * FROM works WHERE id=?', (work_id,)).fetchone()
                        require(work is not None and work['revision'] == item['expected_revision'] and work['lifecycle'] == 'active', 'REVISION_CONFLICT', 'work revision changed')
                        self.evidence_target(item['relationship_source'])
                    else:
                        self.db.execute('INSERT INTO works(id,kind,title,metadata,created_at) VALUES (?,?,?,?,?)',
                                        (work_id, item.get('kind', 'other'), item['title'], '{}', now()))
                    self.db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?,?)', tuple(blob[key] for key in ('hash', 'size', 'path')))
                    self.db.execute('INSERT INTO source_versions VALUES (?,?,?,?,?,?)',
                                    (version_id, work_id, item.get('version_kind', 'snapshot'), now(), item.get('coverage', 'unknown'), encoded({'source': item['source'], 'relationship_source': item.get('relationship_source'), 'acquisition': item.get('acquisition')})))
                    self.db.execute('INSERT INTO version_assets VALUES (?,?,?,?)', (version_id, blob['hash'], 'main', item.get('media_type', 'text/plain')))
                    preferred = self.db.execute('SELECT v.version_kind FROM works w JOIN source_versions v ON v.id=w.preferred_version WHERE w.id=?', (work_id,)).fetchone()
                    new_kind = item.get('version_kind', 'snapshot')
                    if preferred is None or new_kind == 'formal' or (new_kind not in ('supplement', 'preprint') and preferred['version_kind'] != 'formal'):
                        self.db.execute('UPDATE works SET preferred_version=? WHERE id=?', (version_id, work_id))
                    if item.get('work_id'):
                        self.db.execute('UPDATE works SET revision=revision+1 WHERE id=?', (work_id,))
                        for link in self.db.execute('SELECT e.id FROM external_links e JOIN source_versions v ON e.target=\'corpus://version/\'||v.id WHERE v.work_id=?', (work_id,)):
                            self.db.execute('INSERT INTO impact_notices VALUES (?,?,?,?)', (identity('notice'), link['id'], 'direct_source_new_version; wiki not rewritten', now()))
                    self.db.execute('INSERT INTO jobs(id,kind,payload,state,budget,created_at) VALUES (?,?,?,?,?,?)',
                                    (job_id, 'index', encoded({'version_id': version_id, 'pipeline': 'text-v1',
                                        'embedding_profile': self.embedding.profile.id if self.embedding else None,
                                        'embedding_policy': self.embedding.policy_id if self.embedding else None,
                                        'embedding_binding': self.embedding.binding if self.embedding else None,
                                        'embedding_operation': payload['operation_id']}), 'queued', encoded(estimate), now()))
                for identifier in item.get('identifiers', []):
                    lookup_key = normalize_identifier(identifier['scheme'], identifier['raw_value'])
                    require(not identifier_conflict(self.db, identifier['scheme'], lookup_key, work_id),
                            'IDENTITY_REVIEW_REQUIRED', 'identifier belongs to another work; do not silently merge')
                    self.db.execute('INSERT OR IGNORE INTO external_identifiers VALUES (?,?,?,?,?)', (work_id, identifier['scheme'], identifier['raw_value'], lookup_key, identifier['verification_source']))
                for context in item.get('contexts', []):
                    self._link(work_id, context)
                items.append({'work_id': work_id, 'version_id': version_id, 'job_id': job_id, 'original': 'committed', 'index': 'queued_or_existing'})
            return {'items': items, 'estimate': estimate}
        return self.mutate(payload['operation_id'], {'tool': 'ingest', **payload}, save)

    def _link(self, target, context):
        require(self.purge_tombstone('corpus://work/' + target) is None, 'PURGE_BLOCKED', 'permanently deleted work cannot be relinked')
        exists = self.db.execute('SELECT id FROM works WHERE id=? UNION SELECT id FROM research_records WHERE id=?', (target, target)).fetchone()
        require(exists is not None, 'NOT_FOUND', 'link target missing')
        self.db.execute('INSERT OR IGNORE INTO contexts VALUES (?,?,?)', (context, 'project', '[]'))
        self.db.execute('INSERT OR IGNORE INTO context_links VALUES (?,?)', (context, target))

    def link(self, target, context, operation_id):
        def action():
            self._link(target, context)
            return {'target': target, 'context': context}
        return self.mutate(operation_id, {'action': 'link', 'target': target, 'context': context}, action)

    def job(self, job_id):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        require(row is not None, 'NOT_FOUND', 'job not found')
        return dict(row)

    def run_one(self, lease_seconds=60, fail_before_activate=False):
        token = identity('lease')
        with transaction(self.db):
            self.db.execute("UPDATE jobs SET state='cancelled',lease_token=NULL WHERE state='running' AND cancel_requested=1 AND lease_until<?", (time.time(),))
            row = self.db.execute("SELECT * FROM jobs WHERE cancel_requested=0 AND (state='queued' OR (state='running' AND lease_until<?)) ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if row is None:
                return None
            job_id = row['id']
            self.db.execute("UPDATE jobs SET state='running',attempt=attempt+1,lease_token=?,lease_until=? WHERE id=?", (token, time.time() + lease_seconds, job_id))
        try:
            payload = json.loads(row['payload'])
            asset = self.db.execute('SELECT b.*,a.media_type FROM version_assets a JOIN blobs b ON b.hash=a.hash WHERE a.version_id=? AND a.role=\'main\'', (payload['version_id'],)).fetchone()
            original_path = verified_blob(self.root, asset)
            cached = self.db.execute('SELECT * FROM job_parse_cache WHERE job_id=?', (job_id,)).fetchone()
            if cached and cached['pipeline'] == payload['pipeline']:
                text, text_hash, chunks, warnings = json.loads(cached['result'])
            else:
                text, text_hash, chunks, warnings = parse(self.root, original_path, asset['media_type'], asset['hash'], asset['size'])
                with transaction(self.db):
                    current = self.job(job_id)
                    require(current['lease_token'] == token and current['lease_until'] > time.time(), 'LEASE_LOST', 'stale worker cannot checkpoint parsing')
                    self.db.execute('INSERT INTO job_parse_cache VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET pipeline=excluded.pipeline,result=excluded.result',
                                    (job_id, payload['pipeline'], encoded([text, text_hash, chunks, warnings])))
            payload['_job_id'], payload['_lease_token'] = job_id, token
            vectors, vector_status = self.embedding.chunks(chunks, payload) if self.embedding else ([], 'configuration_changed' if payload.get('embedding_profile') else 'not_configured')
            with transaction(self.db):
                current = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                require(current['lease_token'] == token and current['lease_until'] > time.time(), 'LEASE_LOST', 'stale worker cannot activate')
                if current['cancel_requested']:
                    self.db.execute("UPDATE jobs SET state='cancelled',lease_token=NULL WHERE id=?", (job_id,))
                    return self.job(job_id)
                existing = self.db.execute('SELECT id FROM generations WHERE version_id=? AND pipeline=?', (payload['version_id'], payload['pipeline'])).fetchone()
                generation_id = existing['id'] if existing else identity('generation')
                if not existing:
                    self.db.execute('INSERT INTO generations VALUES (?,?,?,?,?,?,?,?)',
                                    (generation_id, payload['version_id'], payload['pipeline'], text, text_hash, 'lexical_ready', encoded(warnings), now()))
                    for ordinal, chunk in enumerate(chunks):
                        chunk_id = identity('chunk')
                        self.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)', (chunk_id, generation_id, ordinal, chunk['text'], encoded(chunk['locator'])))
                        self.db.execute('INSERT INTO chunk_fts VALUES (?,?)', (chunk_id, chunk['text']))
                if vectors:
                    profile = self.embedding.profile
                    self.db.execute('INSERT OR IGNORE INTO embedding_profiles VALUES (?,?)', (profile.id, encoded(profile.config)))
                    generation_chunks = self.db.execute('SELECT id FROM chunks WHERE generation_id=? ORDER BY ordinal', (generation_id,)).fetchall()
                    for chunk, vector in zip(generation_chunks, vectors):
                        self.db.execute('INSERT OR IGNORE INTO chunk_embeddings VALUES (?,?,?)', (chunk['id'], profile.id, encoded(vector)))
                require(not fail_before_activate, 'INJECTED_FAILURE', 'activation fault injection')
                state = 'succeeded' if vector_status in ('complete', 'not_configured') else 'partial'
                active = self.db.execute('SELECT generation_id FROM active_generations WHERE version_id=?', (payload['version_id'],)).fetchone()
                if state == 'succeeded' or active is None:
                    self.db.execute('INSERT INTO active_generations VALUES (?,?) ON CONFLICT(version_id) DO UPDATE SET generation_id=excluded.generation_id', (payload['version_id'], generation_id))
                checkpoint = {'generation_id': generation_id, 'vector_status': vector_status, 'embedded_chunks': len(vectors), 'total_chunks': len(chunks),
                              'capabilities': ['lexical', 'read'] + (['vector_complete'] if vector_status == 'complete' else []),
                              'recovery': 'operator_reconcile_embedding' if vector_status == 'incomplete_ambiguous' else 'corpus_jobs.retry_with_explicit_settings_budget'}
                self.db.execute('UPDATE jobs SET state=?,checkpoint=?,error=NULL,lease_token=NULL WHERE id=?', (state, encoded(checkpoint), job_id))
                self.db.execute('INSERT INTO job_steps VALUES (?,?,?,?,?)', (job_id, current['attempt'], state, encoded(checkpoint), now()))
                audit(self.db, 'generation_activated', {'version_id': payload['version_id'], 'generation_id': generation_id})
            return self.job(job_id)
        except Exception as error:
            with transaction(self.db):
                current = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if current['lease_token'] == token:
                    self.db.execute("UPDATE jobs SET state='failed',error=?,lease_token=NULL WHERE id=?", (getattr(error, 'code', type(error).__name__), job_id))
                    self.db.execute('INSERT OR REPLACE INTO job_steps VALUES (?,?,?,?,?)', (job_id, current['attempt'], 'failed', encoded({'error': getattr(error, 'code', type(error).__name__)}), now()))
            return self.job(job_id)

    def read(self, ref, use_context='browse'):
        require(type(ref) is str and ref.startswith('corpus://'), 'INVALID_REF', 'canonical corpus URI required')
        require(use_context in ('browse', 'report', 'submission'), 'INVALID_CONTEXT', 'unknown use context')
        tombstone = self.purge_tombstone(ref)
        if tombstone is not None:
            return tombstone
        if ref.startswith('corpus://reference/'):
            return self.read_research(ref, use_context)
        segments = ref.removeprefix('corpus://').split('/')
        require(len(segments) == 2, 'INVALID_REF', 'expected stable corpus URI')
        kind, target = segments
        if kind == 'work':
            work = self.db.execute('SELECT * FROM works WHERE id=?', (target,)).fetchone()
            require(work is not None, 'NOT_FOUND', 'work missing')
            canonical = json.loads(work['metadata']).get('merged_into', target)
            if canonical != target:
                result = self.read('corpus://work/' + canonical, use_context)
                result['alias_work'] = dict(work)
                return result
            versions = [dict(row) for row in self.db.execute("SELECT v.* FROM source_versions v JOIN works w ON w.id=v.work_id WHERE w.id=? OR json_extract(w.metadata,'$.merged_into')=? ORDER BY v.acquired_at,v.id", (target, target))]
            result = {'work': dict(work), 'navigation_only': True, 'versions': versions}
            if work['preferred_version']:
                result.update(self.read('corpus://version/' + work['preferred_version'], use_context))
            return result
        if kind == 'version':
            version = self.db.execute('SELECT * FROM source_versions WHERE id=?', (target,)).fetchone()
            require(version is not None, 'NOT_FOUND', 'version missing')
            generation = self.db.execute('SELECT g.* FROM generations g JOIN active_generations a ON a.generation_id=g.id WHERE a.version_id=?', (target,)).fetchone()
            result = {'version': dict(version), 'generation': dict(generation) if generation else None,
                    'coverage': version['coverage'], **self.source_evidence(target)}
            result['assets'] = [dict(row) for row in self.db.execute('SELECT a.hash,b.size,a.role,a.media_type FROM version_assets a JOIN blobs b ON b.hash=a.hash WHERE a.version_id=? ORDER BY a.role,a.hash', (target,))]
            if generation:
                result['warnings'].extend(json.loads(generation['warnings']))
            if use_context != 'browse':
                result['reminders'].append('verify_source_before_formal_use')
            return result
        if kind == 'chunk':
            chunk = self.db.execute('SELECT c.*,g.version_id,g.warnings,v.coverage FROM chunks c JOIN generations g ON g.id=c.generation_id JOIN source_versions v ON v.id=g.version_id WHERE c.id=?', (target,)).fetchone()
            require(chunk is not None, 'NOT_FOUND', 'chunk missing')
            result = {'chunk': dict(chunk), 'locator': json.loads(chunk['locator']), 'fixed_generation': True,
                    'coverage': chunk['coverage'], **self.source_evidence(chunk['version_id'], ref)}
            result['warnings'].extend(json.loads(chunk['warnings']))
            if use_context != 'browse':
                result['reminders'].append('verify_source_before_formal_use')
            return result
        raise CorpusError('INVALID_REF', 'unsupported reference kind')

    def expand_chunk(self, ref, before=1, after=1, use_context='browse'):
        require(type(before) is int and type(after) is int and 0 <= before <= 5 and 0 <= after <= 5, 'INVALID_ARGUMENT', 'chunk expansion bounds 0..5')
        result = self.read(ref, use_context)
        require('chunk' in result, 'INVALID_REF', 'expansion requires precise chunk URI')
        chunk = result['chunk']
        result['context'] = [dict(row) for row in self.db.execute('SELECT * FROM chunks WHERE generation_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',
            (chunk['generation_id'], chunk['ordinal'] - before, chunk['ordinal'] + after))]
        return result
