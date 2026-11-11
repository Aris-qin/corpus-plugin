"""Synthetic protocol fixtures only: no live vendor verification."""
import json

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus
from test_retrieval import seed


def config(**changes):
    result = {'protocol': 'dashscope_native',
              'endpoint': 'https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank',
              'exact_model': 'qwen3-rerank', 'key_env': 'SYNTHETIC_RERANK_KEY',
              'timeout_seconds': 4, 'candidate_budget': 3, 'max_documents': 3,
              'max_calls': 2, 'max_chars': 10000, 'max_document_chars': 2000}
    return {**result, **changes}


def configured(root, rerank=None, transport=None, test_mode=True):
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'SYNTHETIC-settings.json'
    path.write_text(json.dumps({'data_root': str(root / 'data'), 'embedding': None,
                                'rerank': config() if rerank is None else rerank}))
    return Corpus.from_settings(path, test_mode=test_mode, rerank_transport=transport)


def response(payload):
    documents = payload.get('input', payload)['documents']
    results = [{'index': index, 'relevance_score': float(index)} for index in reversed(range(len(documents)))]
    return {'output': {'results': results}} if 'input' in payload else {'results': results}


@pytest.mark.parametrize('protocol', ['dashscope_native', 'openai_compatible'])
def test_synthetic_protocol_order_evidence_and_pagination(root, protocol):
    calls = []
    def transport(payload):
        calls.append(payload)
        assert not corpus.db.in_transaction
        return response(payload)
    corpus = configured(root, config(protocol=protocol), transport)
    seed(corpus, 'SYNTHETIC evidence first\nevidence second\nevidence third')
    baseline = Corpus(corpus.root).search('evidence')['hits']
    first = corpus.search('evidence', top_k=1)
    second = corpus.search('evidence', top_k=1, cursor=first['cursor'])
    assert first['hits'][0]['ref'] == baseline[-1]['ref']
    assert second['hits'][0]['ref'] == baseline[-2]['ref']
    assert first['hits'][0]['locator'] == baseline[-1]['locator']
    assert first['hits'][0]['warnings'] == baseline[-1]['warnings']
    assert 'rerank_complete' in first['warnings'] and len(calls) == 1
    payload = calls[0]
    assert payload['model'] == 'qwen3-rerank'
    if protocol == 'dashscope_native':
        assert set(payload) == {'model', 'input', 'parameters'}
        assert payload['parameters'] == {'top_n': 3, 'return_documents': False}
        assert payload['input'] == {'query': 'evidence', 'documents': [hit['text'] for hit in baseline]}
    else:
        assert set(payload) == {'model', 'query', 'documents', 'top_n'}
    assert corpus.search('evidence', scope='linked', context='absent')['hits'] == []
    assert len(calls) == 1


@pytest.mark.parametrize('change', [dict(protocol='guess'), dict(endpoint='http://example.test/rerank'),
    dict(endpoint='https://user:password@example.test/rerank'), dict(exact_model=''), dict(key_env='bad-key'),
    dict(timeout_seconds=True), dict(timeout_seconds=61), dict(timeout_seconds=10 ** 400), dict(candidate_budget=0), dict(max_documents=101),
    dict(max_calls=0), dict(max_chars=0), dict(max_document_chars=False), dict(extra=True)])
def test_strict_config(root, change):
    with pytest.raises(CorpusError):
        configured(root, config(**change))


@pytest.mark.parametrize('bad', [None, [], {}, {'output': []}, {'output': {'results': []}},
    {'output': {'results': [{'index': True, 'relevance_score': 1}]}},
    {'output': {'results': [{'index': -1, 'relevance_score': 1}]}},
    {'output': {'results': [{'index': 0, 'relevance_score': float('nan')}]}},
    {'output': {'results': [{'index': 0, 'relevance_score': 10 ** 400}]}},
    {'output': {'results': [{'index': 0, 'relevance_score': 1}, {'index': 0, 'relevance_score': 2}]}}])
def test_malformed_response_falls_back(root, bad):
    corpus = configured(root, transport=lambda payload: bad)
    seed(corpus)
    baseline = Corpus(corpus.root).search('evidence')['hits']
    result = corpus.search('evidence')
    assert result['hits'] == baseline
    assert 'rerank_degraded_provider_response' in result['warnings']
    assert 'rerank_complete' not in result['warnings']


@pytest.mark.parametrize('error', [TimeoutError('SYNTHETIC'), OSError('SYNTHETIC')])
def test_outage_is_cached_without_retry(root, error):
    calls = []
    def transport(payload):
        calls.append(payload)
        raise error
    corpus = configured(root, transport=transport)
    seed(corpus)
    first = corpus.search('evidence')
    assert corpus.search('evidence') == first and len(calls) == 1
    assert 'rerank_degraded_provider_failed' in first['warnings']


def test_missing_key_and_offline_no_dns(root, monkeypatch):
    corpus = configured(root, test_mode=False)
    seed(corpus)
    monkeypatch.delenv('SYNTHETIC_RERANK_KEY', raising=False)
    monkeypatch.delenv('CORPUS_OFFLINE')
    assert 'rerank_degraded_missing_key' in corpus.search('evidence')['warnings']
    monkeypatch.setenv('SYNTHETIC_RERANK_KEY', 'SYNTHETIC-NOT-A-REAL-KEY')
    monkeypatch.setenv('CORPUS_OFFLINE', '1')
    assert 'rerank_degraded_network_disabled' in corpus.search('SYNTHETIC')['warnings']


def test_persistent_budgets_and_candidate_limits(root):
    calls = []
    def transport(payload):
        calls.append(payload)
        return response(payload)
    corpus = configured(root, config(candidate_budget=1, max_calls=1), transport)
    seed(corpus, 'SYNTHETIC evidence first\nevidence second')
    assert 'rerank_candidate_limit' in corpus.search('evidence')['warnings']
    assert len(calls[0]['input']['documents']) == 1
    corpus.close()
    corpus = configured(root, config(candidate_budget=1, max_calls=1), transport)
    assert 'rerank_degraded_budget_exhausted' in corpus.search('second')['warnings']
    assert len(calls) == 1


@pytest.mark.parametrize('limits', [dict(max_chars=1), dict(max_document_chars=1)])
def test_character_limits_never_send(root, limits):
    def forbidden(payload):
        raise AssertionError('over-budget document sent')
    corpus = configured(root, config(**limits), forbidden)
    seed(corpus)
    assert 'rerank_degraded_budget_exhausted' in corpus.search('evidence')['warnings']


def test_fixture_transport_forbidden_outside_test_mode(root):
    with pytest.raises(CorpusError) as failure:
        configured(root, transport=response, test_mode=False)
    assert failure.value.code == 'TEST_ADAPTER_FORBIDDEN'


def test_legacy_and_null_configuration(root):
    root.mkdir(parents=True)
    path = root / 'SYNTHETIC-legacy.json'
    payload = {'data_root': str(root / 'data'), 'embedding': None}
    path.write_text(json.dumps(payload))
    corpus = Corpus.from_settings(path)
    seed(corpus)
    baseline = corpus.search('evidence')
    corpus.close()
    path.write_text(json.dumps({**payload, 'rerank': None}))
    corpus = Corpus.from_settings(path)
    assert corpus.search('evidence') == baseline


@pytest.mark.parametrize('top_k', [0, True, 101])
def test_invalid_limit_never_bills(root, top_k):
    calls = []
    corpus = configured(root, transport=lambda payload: calls.append(payload) or response(payload))
    seed(corpus)
    with pytest.raises(CorpusError):
        corpus.search('evidence', top_k=top_k)
    assert not calls


def test_changed_cursor_binding_never_bills(root):
    calls = []
    corpus = configured(root, transport=lambda payload: calls.append(payload) or response(payload))
    seed(corpus, 'SYNTHETIC evidence one\nevidence two')
    first = corpus.search('evidence', top_k=1)
    with pytest.raises(CorpusError):
        corpus.search('SYNTHETIC', top_k=1, cursor=first['cursor'])
    assert len(calls) == 1


@pytest.mark.parametrize('protocol', ['dashscope_native', 'openai_compatible'])
def test_synthetic_json_transport_authorization_timeout_and_bounds(root, monkeypatch, protocol):
    import corpus_core.network as network
    original = network.validate_url
    calls = []
    def request(url, host, address, max_bytes, method, body, headers, timeout):
        assert url == config()['endpoint'] and address == '93.184.216.34'
        assert method == 'POST' and timeout == 4 and max_bytes == 1024 * 1024
        assert headers['Authorization'] == 'Bearer SYNTHETIC-NOT-A-REAL-KEY'
        payload = json.loads(body)
        calls.append(payload)
        return 200, {}, json.dumps(response(payload)).encode()
    monkeypatch.delenv('CORPUS_OFFLINE')
    monkeypatch.setenv('SYNTHETIC_RERANK_KEY', 'SYNTHETIC-NOT-A-REAL-KEY')
    monkeypatch.setattr(network, 'validate_url', lambda endpoint: original(endpoint, resolver=lambda host, port: ['93.184.216.34']))
    monkeypatch.setattr(network, 'request', request)
    corpus = configured(root, config(protocol=protocol), test_mode=False)
    seed(corpus)
    assert 'rerank_complete' in corpus.search('evidence')['warnings']
    assert len(calls) == 1


def test_private_dns_is_rejected_before_post(root, monkeypatch):
    import corpus_core.network as network
    original = network.validate_url
    monkeypatch.delenv('CORPUS_OFFLINE')
    monkeypatch.setenv('SYNTHETIC_RERANK_KEY', 'SYNTHETIC-NOT-A-REAL-KEY')
    monkeypatch.setattr(network, 'validate_url', lambda endpoint: original(endpoint, resolver=lambda host, port: ['127.0.0.1']))
    corpus = configured(root, test_mode=False)
    seed(corpus)
    assert 'rerank_degraded_unsafe_url' in corpus.search('evidence')['warnings']


def test_hybrid_scoped_filters_status_and_recycled_sources(root):
    from corpus_core.embeddings import Profile
    calls = []
    corpus = configured(root, transport=lambda payload: calls.append(payload) or response(payload))
    allowed = seed(corpus, 'SYNTHETIC allowed evidence', 'allowed')
    excluded = seed(corpus, 'SYNTHETIC forbidden evidence', 'excluded')
    recycled = seed(corpus, 'SYNTHETIC recycled evidence', 'recycled')
    corpus.lifecycle(recycled['work_id'], 'recycled', 1, 'recycle')
    corpus.link(allowed['work_id'], 'SYNTHETIC-context', 'link')
    ref = 'corpus://version/' + allowed['version_id']
    corpus.metadata(allowed['work_id'], 1, {'retraction_status': 'retracted'}, ref, 'retract')
    profile = Profile('mock', 'SYNTHETIC', 2)
    corpus.index_vectors(profile, lambda texts: [[1.0, 0.0] for text in texts], test_root=root)
    result = corpus.search('semantic', scope='linked', context='SYNTHETIC-context', profile=profile,
                           query_vector=[1.0, 0.0], filters={'version_ids': [allowed['version_id']]})
    assert result['vector_candidates'] == 1 and len(result['hits']) == 1
    assert result['hits'][0]['version_id'] == allowed['version_id']
    assert 'source_retracted' in result['hits'][0]['warnings']
    assert calls[0]['input']['documents'] == ['SYNTHETIC allowed evidence']
    assert excluded['version_id'] != allowed['version_id']


def test_formal_preferred_precedence_survives_scores(root):
    corpus = configured(root, transport=response)
    old = seed(corpus)
    new = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC formal',
        'content': 'SYNTHETIC formal evidence', 'work_id': old['work_id'], 'expected_revision': 1,
        'version_kind': 'formal', 'relationship_source': 'corpus://version/' + old['version_id']}],
        'operation_id': 'formal'})['items'][0]
    assert corpus.run_one()['state'] == 'succeeded'
    hits = corpus.search('evidence')['hits']
    assert hits[0]['version_id'] == new['version_id'] and hits[0]['preferred']
    assert hits[0]['rerank_score'] < hits[1]['rerank_score']


def test_concurrent_snapshot_reservation_prevents_duplicate_call(root):
    calls = []
    concurrent = []
    def transport(payload):
        calls.append(payload)
        other = configured(root, transport=response)
        concurrent.append(other.search('evidence'))
        other.close()
        return response(payload)
    corpus = configured(root, transport=transport)
    seed(corpus)
    assert 'rerank_complete' in corpus.search('evidence')['warnings']
    assert len(calls) == 1
    assert 'rerank_degraded_inflight' in concurrent[0]['warnings']


@pytest.mark.parametrize('status,content,warning', [
    (503, b'{}', 'provider_retryable'), (302, b'{}', 'provider_failed'),
    (200, b'not JSON', 'provider_failed')])
def test_http_failures_do_not_retry_or_follow_redirects(root, monkeypatch, status, content, warning):
    import corpus_core.network as network
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        return status, {'location': 'https://unrelated.example.test/rerank'}, content
    monkeypatch.delenv('CORPUS_OFFLINE')
    monkeypatch.setenv('SYNTHETIC_RERANK_KEY', 'SYNTHETIC-NOT-A-REAL-KEY')
    monkeypatch.setattr(network, 'validate_url', lambda endpoint: (endpoint, 'SYNTHETIC.test', '93.184.216.34'))
    monkeypatch.setattr(network, 'request', request)
    corpus = configured(root, test_mode=False)
    seed(corpus)
    assert 'rerank_degraded_' + warning in corpus.search('evidence')['warnings']
    corpus.search('evidence')
    assert len(calls) == 1


def test_response_size_bound_before_read(root, monkeypatch):
    import corpus_core.network as network
    from corpus_core.embeddings import JSONAPITransport
    class SyntheticConnection:
        def __init__(self, host, address, timeout):
            assert timeout == 4
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return self
        def getheader(self, name):
            return str(1024 * 1024 + 1)
        def read(self, count):
            raise AssertionError('oversized declared response must not be read')
        def close(self):
            pass
    monkeypatch.delenv('CORPUS_OFFLINE')
    monkeypatch.setattr(network, 'PinnedHTTPS', SyntheticConnection)
    monkeypatch.setattr(network, 'validate_url', lambda endpoint: (endpoint, 'SYNTHETIC.test', '93.184.216.34'))
    with pytest.raises(CorpusError) as failure:
        JSONAPITransport(config()['endpoint'], max_response_bytes=1024 * 1024, timeout=4)({})
    assert failure.value.code == 'RESPONSE_LIMIT'
