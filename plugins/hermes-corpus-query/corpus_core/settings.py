import json
import math
import os
import re

from .db import digest, encoded, transaction
from .embeddings import APIEmbedding, JSONAPITransport, Profile
from .errors import CorpusError, require
from .paths import bounded, sandbox_root


def load_settings(path, authorized_root=None):
    boundary = sandbox_root(authorized_root)
    path = bounded(path, boundary)
    require(path.is_file() and path.stat().st_size <= 65536, 'INVALID_SETTINGS', 'explicit bounded nonsecret settings file required')
    try:
        settings = json.loads(path.read_text())
    except (ValueError, UnicodeError) as error:
        raise CorpusError('INVALID_SETTINGS', 'settings must be JSON') from error
    require(type(settings) is dict and {'data_root', 'embedding'} <= set(settings) <= {'data_root', 'embedding', 'rerank', 'safety_mode'}, 'INVALID_SETTINGS', 'settings fields must be data_root, embedding and optional rerank/safety_mode')
    require(settings.get('safety_mode', 'disabled') == 'disabled', 'INVALID_SETTINGS', 'INVALID_SETTINGS: host approval unavailable; sensitive actions must remain disabled')
    require(type(settings['data_root']) is str and bool(settings['data_root']), 'INVALID_SETTINGS', 'explicit data_root path required')
    bounded(settings['data_root'], boundary)
    return settings


class EmbeddingRuntime:
    def __init__(self, corpus, settings, transport=None):
        require(type(settings) is dict and set(settings) == {'profile', 'endpoint', 'key_env', 'policy'}, 'INVALID_SETTINGS', 'embedding settings fields mismatch')
        require(transport is None or corpus.test_mode, 'TEST_ADAPTER_FORBIDDEN', 'offline transport fixtures require explicit test mode')
        profile = settings['profile']
        require(type(profile) is dict and {'provider', 'exact_model', 'dim'} <= set(profile) <= {'provider', 'exact_model', 'dim', 'metric', 'preprocessing_version', 'provenance'}, 'INVALID_PROFILE', 'strict profile fields required')
        require(profile['provider'] != 'mock', 'MOCK_FORBIDDEN', 'configured API path never accepts mock profiles')
        self.profile = Profile(**profile)
        self.profile.validate()
        from .identity import normalize_identifier
        from urllib.parse import urlsplit
        self.endpoint = normalize_identifier('url', settings['endpoint'])
        parts = urlsplit(self.endpoint)
        require(parts.scheme == 'https' and parts.port in (443, None), 'INVALID_SETTINGS', 'embedding endpoint must be public HTTPS; DNS checked at request time')
        require(type(settings['key_env']) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,100}', settings['key_env']), 'INVALID_SETTINGS', 'only explicit key environment variable name accepted')
        self.key_env = settings['key_env']
        policy = settings['policy']
        require(type(policy) is dict and set(policy) == {'operation_ids', 'max_calls', 'max_chars', 'batch_size', 'max_cost_usd', 'max_cost_per_call_usd'}, 'INVALID_POLICY', 'explicit bounded scope and cost policy required')
        require(type(policy['operation_ids']) is list and 1 <= len(policy['operation_ids']) <= 100 and all(type(operation) is str and 0 < len(operation) <= 200 for operation in policy['operation_ids']), 'INVALID_POLICY', 'explicit submission operation IDs required; no full reembedding')
        for field, maximum in (('max_calls', 10000), ('max_chars', 10000000), ('batch_size', 100)):
            require(type(policy[field]) is int and 1 <= policy[field] <= maximum, 'INVALID_POLICY', field + ' must be explicitly bounded')
        for field in ('max_cost_usd', 'max_cost_per_call_usd'):
            require(type(policy[field]) in (int, float) and math.isfinite(policy[field]) and policy[field] > 0, 'INVALID_POLICY', 'positive known cost ceiling required')
        self.policy = policy
        self.policy_id = digest({'profile': self.profile.id, 'endpoint': self.endpoint, 'key_env': self.key_env, 'policy': policy})
        self.binding = digest({'profile': self.profile.id, 'endpoint': self.endpoint, 'key_env': self.key_env, 'batch_size': policy['batch_size']})
        self.corpus = corpus
        self.fixture_transport = transport

    def transport(self, payload):
        if self.fixture_transport is not None:
            return self.fixture_transport(payload)
        require(os.environ.get('CORPUS_OFFLINE') != '1', 'NETWORK_DISABLED', 'offline profile forbids provider calls including DNS')
        key = os.environ.get(self.key_env)
        require(key is not None and bool(key), 'MISSING_KEY', 'configured provider key environment variable is unset')
        return JSONAPITransport(self.endpoint, 'Bearer ' + key)(payload)

    def batch(self, texts, key):
        database = self.corpus.db
        chars = sum(len(text.encode()) for text in texts)
        batch_id = digest({'profile': self.profile.id, 'key': key, 'texts': texts})
        with transaction(database):
            old = database.execute('SELECT * FROM embedding_batches WHERE id=?', (batch_id,)).fetchone()
            if old:
                if old['state'] == 'succeeded':
                    return json.loads(old['vectors']), 'complete'
                if old['state'] != 'not_sent':
                    return None, 'incomplete_ambiguous'
            ambiguous = database.execute("SELECT 1 FROM embedding_batches WHERE profile_id=? AND state IN ('started','ambiguous') LIMIT 1", (self.profile.id,)).fetchone()
            if ambiguous:
                return None, 'incomplete_ambiguous'
            spent = database.execute("SELECT count(*),coalesce(sum(input_chars),0) FROM embedding_batches WHERE policy_id=? AND state!='not_sent'", (self.policy_id,)).fetchone()
            if (spent[0] >= self.policy['max_calls'] or spent[1] + chars > self.policy['max_chars'] or
                    (spent[0] + 1) * self.policy['max_cost_per_call_usd'] > self.policy['max_cost_usd']):
                return None, 'budget_exhausted'
            if old:
                database.execute("UPDATE embedding_batches SET policy_id=?,state='started',error=NULL WHERE id=?", (self.policy_id, batch_id))
            else:
                database.execute('INSERT INTO embedding_batches VALUES (?,?,?,?,?,?,?)', (batch_id, self.policy_id, self.profile.id, 'started', chars, None, None))
                database.execute('INSERT INTO embedding_requests VALUES (?,?,?)', (batch_id, len(texts), encoded(key)))
        try:
            vectors = APIEmbedding(self.profile, self.transport, max_texts=self.policy['batch_size'], max_chars=self.policy['max_chars'])(texts)
        except Exception as error:
            not_sent = self.fixture_transport is None and isinstance(error, CorpusError) and error.code in ('NETWORK_DISABLED', 'MISSING_KEY', 'INVALID_AUTH', 'UNSAFE_URL', 'PROVIDER_NOT_SENT')
            with transaction(database):
                current = database.execute('SELECT * FROM embedding_batches WHERE id=?', (batch_id,)).fetchone()
                if current['state'] == 'succeeded':
                    return json.loads(current['vectors']), 'complete'
                database.execute("UPDATE embedding_batches SET state=?,error=? WHERE id=? AND state='started'", ('not_sent' if not_sent else 'ambiguous', getattr(error, 'code', type(error).__name__), batch_id))
                from .db import audit
                audit(database, 'embedding_request_not_sent' if not_sent else 'embedding_request_ambiguous', {'batch_id': batch_id, 'error': getattr(error, 'code', type(error).__name__)})
            return None, 'provider_not_sent' if not_sent else 'incomplete_ambiguous'
        with transaction(database):
            current = database.execute('SELECT * FROM embedding_batches WHERE id=?', (batch_id,)).fetchone()
            if current['state'] == 'succeeded':
                require(json.loads(current['vectors']) == vectors, 'RECONCILIATION_CONFLICT', 'late provider response conflicts with controlled recovered vectors; review without overwriting')
            else:
                require(current['state'] == 'started', 'RECONCILIATION_CONFLICT', 'batch changed while provider request was running')
                database.execute("UPDATE embedding_batches SET state='succeeded',vectors=? WHERE id=?", (encoded(vectors), batch_id))
        return vectors, 'complete'

    def reconcile(self, manifest):
        require(type(manifest) is dict and set(manifest) == {'batch_id', 'profile_id', 'vectors', 'evidence'}, 'INVALID_RECONCILIATION', 'exact batch/profile/recovered vectors/evidence required')
        require(type(manifest['evidence']) is str and 1 <= len(manifest['evidence']) <= 2000, 'INVALID_RECONCILIATION', 'provider recovery evidence reference required; local operator assertion, not verified human identity')
        require(manifest['profile_id'] == self.profile.id, 'INVALID_RECONCILIATION', 'profile mismatch')
        with transaction(self.corpus.db):
            row = self.corpus.db.execute('SELECT b.*,r.input_count FROM embedding_batches b JOIN embedding_requests r ON r.batch_id=b.id WHERE b.id=?', (manifest['batch_id'],)).fetchone()
            require(row is not None and row['profile_id'] == self.profile.id, 'INVALID_RECONCILIATION', 'known batch required; historical unknown requests require parent review')
            vectors = manifest['vectors']
            require(type(vectors) is list and len(vectors) == row['input_count'], 'INVALID_RECONCILIATION', 'recovered vector count mismatch')
            for vector in vectors:
                self.profile.vector(vector)
            if row['state'] == 'succeeded':
                require(json.loads(row['vectors']) == vectors, 'INVALID_RECONCILIATION', 'reconciliation conflicts with completed batch')
                return {'state': 'succeeded', 'batch_id': row['id'], 'replayed': True}
            import time
            active = self.corpus.db.execute("SELECT 1 FROM jobs WHERE state='running' AND lease_until>? AND json_extract(payload,'$.embedding_profile')=? LIMIT 1", (time.time(), self.profile.id)).fetchone()
            require(row['state'] in ('ambiguous', 'started') and active is None, 'INVALID_RECONCILIATION', 'active/unknown batch cannot be reconciled; wait for job lease expiry')
            self.corpus.db.execute("UPDATE embedding_batches SET state='succeeded',vectors=?,error=NULL WHERE id=?", (encoded(vectors), row['id']))
            from .db import audit
            audit(self.corpus.db, 'local_operator_embedding_reconciliation', {'batch_id': row['id'], 'manifest_hash': digest(manifest), 'identity_verified': False})
            return {'state': 'succeeded', 'batch_id': row['id'], 'replayed': False}

    def recovery_inventory(self):
        return {'policy_id': self.policy_id, 'policy': self.policy,
                'batches': [dict(row) for row in self.corpus.db.execute('SELECT b.id,b.policy_id,b.state,b.input_chars,b.error,r.input_count,r.request_key FROM embedding_batches b LEFT JOIN embedding_requests r ON r.batch_id=b.id WHERE b.profile_id=? ORDER BY b.id', (self.profile.id,))],
                'recovery': 'reconcile-embedding accepts recovered provider vectors only; uncertain billing never authorizes another request; old requests without count require parent review'}

    def chunks(self, chunks, payload):
        if payload.get('embedding_profile') != self.profile.id or payload.get('embedding_policy') != self.policy_id:
            return [], 'configuration_changed'
        if payload.get('embedding_operation') not in self.policy['operation_ids']:
            return [], 'scope_not_approved'
        result = []
        batch_size = self.policy['batch_size']
        for offset in range(0, len(chunks), batch_size):
            with transaction(self.corpus.db):
                import time
                job = self.corpus.db.execute('SELECT * FROM jobs WHERE id=?', (payload['_job_id'],)).fetchone()
                require(job['lease_token'] == payload['_lease_token'] and job['lease_until'] > time.time() and not job['cancel_requested'], 'LEASE_LOST', 'stale/cancelled embedding worker cannot spend')
                self.corpus.db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time() + 60, payload['_job_id']))
            selected = chunks[offset:offset + batch_size]
            key = {'version': payload['version_id'], 'pipeline': payload['pipeline'], 'offset': offset}
            vectors, status = self.batch([chunk['text'] for chunk in selected], key)
            if vectors is None:
                return result, status
            result.extend(vectors)
        return result, 'complete'

    def query(self, text):
        vectors, status = self.batch([text], {'query': text})
        return (vectors[0] if vectors else None), status
