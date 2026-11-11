import json
import math
import os
import re
import time
from urllib.parse import urlsplit

from .db import digest, encoded, transaction
from .embeddings import JSONAPITransport
from .errors import CorpusError, require
from .identity import normalize_identifier


class RerankRuntime:
    def __init__(self, corpus, settings, transport=None):
        fields = {'protocol', 'endpoint', 'exact_model', 'key_env', 'timeout_seconds',
                  'candidate_budget', 'max_documents', 'max_calls', 'max_chars', 'max_document_chars'}
        require(type(settings) is dict and set(settings) == fields, 'INVALID_SETTINGS', 'strict explicit rerank fields required')
        require(transport is None or corpus.test_mode, 'TEST_ADAPTER_FORBIDDEN', 'rerank fixtures require explicit test mode')
        require(settings['protocol'] in ('openai_compatible', 'dashscope_native'), 'INVALID_SETTINGS', 'explicit rerank protocol required')
        require(type(settings['exact_model']) is str and 0 < len(settings['exact_model']) <= 500 and settings['exact_model'].strip() == settings['exact_model'],
                'INVALID_SETTINGS', 'exact rerank model required')
        require(type(settings['key_env']) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,100}', settings['key_env']),
                'INVALID_SETTINGS', 'explicit rerank key environment name required')
        endpoint = normalize_identifier('url', settings['endpoint'])
        parts = urlsplit(endpoint)
        require(parts.scheme == 'https' and parts.port in (443, None) and not parts.fragment,
                'INVALID_SETTINGS', 'rerank endpoint must be public HTTPS; DNS pinned at request time')
        for field, maximum in (('candidate_budget', 100), ('max_documents', 100), ('max_calls', 10000),
                               ('max_chars', 10000000), ('max_document_chars', 100000)):
            require(type(settings[field]) is int and 1 <= settings[field] <= maximum,
                    'INVALID_SETTINGS', field + ' must be explicitly bounded')
        require(type(settings['timeout_seconds']) in (int, float) and 0 < settings['timeout_seconds'] <= 60,
                'INVALID_SETTINGS', 'rerank timeout must be positive and at most 60 seconds')
        self.settings = {**settings, 'endpoint': endpoint}
        self.id = digest(self.settings)
        self.corpus = corpus
        self.fixture_transport = transport
        corpus.db.execute('CREATE TABLE IF NOT EXISTS rerank_budgets (id TEXT PRIMARY KEY,calls INTEGER NOT NULL,chars INTEGER NOT NULL)')
        corpus.db.execute('CREATE TABLE IF NOT EXISTS rerank_snapshots (id TEXT PRIMARY KEY,created REAL NOT NULL,result TEXT NOT NULL)')

    def transport(self, payload):
        if self.fixture_transport is not None:
            return self.fixture_transport(payload)
        key = os.environ.get(self.settings['key_env'])
        require(bool(key), 'MISSING_KEY', 'configured rerank key environment variable is unset')
        require(os.environ.get('CORPUS_OFFLINE') != '1', 'NETWORK_DISABLED', 'offline forbids rerank DNS and requests')
        return JSONAPITransport(self.settings['endpoint'], 'Bearer ' + key, max_response_bytes=1024 * 1024,
                                timeout=self.settings['timeout_seconds'])(payload)

    def normalize(self, response, count):
        require(type(response) is dict, 'PROVIDER_RESPONSE', 'rerank response must be an object')
        require(response.get('model', self.settings['exact_model']) == self.settings['exact_model'],
                'PROVIDER_RESPONSE', 'rerank returned a different model')
        output = response.get('output') if self.settings['protocol'] == 'dashscope_native' else response
        require(type(output) is dict and type(output.get('results')) is list,
                'PROVIDER_RESPONSE', 'rerank results missing')
        results = output['results']
        require(len(results) == count, 'PROVIDER_RESPONSE', 'rerank must score all submitted candidates')
        seen = set()
        scores = {}
        for result in results:
            require(type(result) is dict, 'PROVIDER_RESPONSE', 'rerank result must be an object')
            index, score = result.get('index'), result.get('relevance_score')
            require(type(index) is int and 0 <= index < count and index not in seen,
                    'PROVIDER_RESPONSE', 'rerank index invalid or duplicated')
            try:
                finite = type(score) in (int, float) and math.isfinite(score)
            except OverflowError:
                finite = False
            require(finite, 'PROVIDER_RESPONSE', 'rerank score must be finite')
            seen.add(index)
            scores[index] = score
        return scores

    def rank(self, query, hits, binding):
        if not hits:
            return hits, []
        settings = self.settings
        count = min(len(hits), settings['candidate_budget'], settings['max_documents'])
        candidates = hits[:count]
        warnings = ['rerank_candidate_limit'] if count < len(hits) else []
        cache_id = digest({'config': self.id, 'binding': binding, 'hits': hits})
        database = self.corpus.db
        with transaction(database):
            database.execute('DELETE FROM rerank_snapshots WHERE created<?', (time.time() - 900,))
            cached = database.execute('SELECT result FROM rerank_snapshots WHERE id=?', (cache_id,)).fetchone()
            if cached:
                return self.apply(hits, json.loads(cached['result']), warnings)
            result = {'warning': 'rerank_degraded_inflight', 'scores': None}
            database.execute('INSERT INTO rerank_snapshots VALUES (?,?,?)', (cache_id, time.time(), encoded(result)))
        try:
            documents = [hit['text'] for hit in candidates]
            chars = len(query) + sum(map(len, documents))
            require(all(len(document) <= settings['max_document_chars'] for document in documents),
                    'BUDGET_EXHAUSTED', 'rerank document character budget exceeded')
            with transaction(database):
                database.execute('INSERT OR IGNORE INTO rerank_budgets VALUES (?,0,0)', (self.id,))
                budget = database.execute('SELECT * FROM rerank_budgets WHERE id=?', (self.id,)).fetchone()
                require(budget['calls'] < settings['max_calls'] and budget['chars'] + chars <= settings['max_chars'],
                        'BUDGET_EXHAUSTED', 'rerank cumulative call/character budget exhausted')
                database.execute('UPDATE rerank_budgets SET calls=calls+1,chars=chars+? WHERE id=?', (chars, self.id))
            if settings['protocol'] == 'dashscope_native':
                payload = {'model': settings['exact_model'], 'input': {'query': query, 'documents': documents},
                           'parameters': {'top_n': count, 'return_documents': False}}
            else:
                payload = {'model': settings['exact_model'], 'query': query, 'documents': documents, 'top_n': count}
            scores = self.normalize(self.transport(payload), count)
            result = {'warning': 'rerank_complete', 'scores': [scores[index] for index in range(count)]}
        except (CorpusError, OSError, ValueError, TypeError, KeyError) as error:
            code = error.code if isinstance(error, CorpusError) else 'PROVIDER_FAILED'
            result = {'warning': 'rerank_degraded_' + code.lower(), 'scores': None}
        with transaction(database):
            database.execute('UPDATE rerank_snapshots SET result=? WHERE id=?', (encoded(result), cache_id))
        return self.apply(hits, result, warnings)

    def apply(self, hits, result, warnings):
        scores = result['scores']
        if scores is None:
            return hits, warnings + [result['warning']]
        candidates = [{**hit, 'rerank_score': score} for hit, score in zip(hits, scores)]
        candidates.sort(key=lambda hit: (hit['version_kind'] != 'formal', not hit['preferred'], -hit['rerank_score'], hit['ref']))
        return candidates + hits[len(scores):], warnings + [result['warning']]
