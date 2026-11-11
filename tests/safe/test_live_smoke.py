"""Synthetic transport tests only; never execute the live utility."""
import json

import pytest

from corpus_core.errors import CorpusError


def smoke_settings(root):
    root.mkdir(parents=True)
    path = root / 'SYNTHETIC-settings.json'
    settings = {'data_root': str(root / 'new-smoke-data'), 'embedding': {
        'profile': {'provider': 'dashscope', 'exact_model': 'text-embedding-v4', 'dim': 1024},
        'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings', 'key_env': 'SYNTHETIC_UNUSED_KEY',
        'policy': {'operation_ids': ['live-smoke'], 'max_calls': 1, 'max_chars': 1024, 'batch_size': 1,
                   'max_cost_usd': 0.01, 'max_cost_per_call_usd': 0.01}}, 'rerank': {
        'protocol': 'dashscope_native', 'endpoint': 'https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank',
        'exact_model': 'qwen3-rerank', 'key_env': 'SYNTHETIC_UNUSED_KEY', 'timeout_seconds': 5,
        'candidate_budget': 3, 'max_documents': 3, 'max_calls': 1, 'max_chars': 1024, 'max_document_chars': 200}}
    path.write_text(json.dumps(settings))
    return path, settings


def test_bounded_smoke_exercises_candidate_adapters_only_synthetic(root, monkeypatch):
    from corpus_core.live_smoke import bounded_smoke
    from corpus_core.settings import EmbeddingRuntime
    from corpus_core.rerank import RerankRuntime
    path, settings = smoke_settings(root)
    calls = []
    def embedding(runtime, payload):
        calls.append(('embedding', payload))
        return {'data': [{'index': 0, 'embedding': [1.0] + [0.0] * 1023}]}
    def rerank(runtime, payload):
        calls.append(('rerank', payload))
        return {'output': {'results': [{'index': index, 'relevance_score': 1 / (index + 1)} for index in range(3)]}}
    monkeypatch.setattr(EmbeddingRuntime, 'transport', embedding)
    monkeypatch.setattr(RerankRuntime, 'transport', rerank)
    result = bounded_smoke(path, root.parent)
    assert result['embedding'] == 'complete' and result['rerank'] == 'rerank_complete'
    assert len(calls) == 2
    assert calls[0][1]['model'] == 'text-embedding-v4' and calls[0][1]['dimensions'] == 1024
    assert len(calls[0][1]['input']) == 1
    assert calls[1][1]['model'] == 'qwen3-rerank' and len(calls[1][1]['input']['documents']) == 3
    with pytest.raises(CorpusError):
        bounded_smoke(path, root.parent)


@pytest.mark.parametrize('change', ['model', 'budget', 'endpoint'])
def test_smoke_rejects_unbounded_config_before_calls(root, change):
    from corpus_core.live_smoke import bounded_smoke
    path, settings = smoke_settings(root)
    if change == 'model':
        settings['embedding']['profile']['exact_model'] = 'other'
    elif change == 'budget':
        settings['rerank']['max_calls'] = 100
    else:
        settings['rerank']['endpoint'] = 'https://example.test/rerank'
    path.write_text(json.dumps(settings))
    with pytest.raises(CorpusError):
        bounded_smoke(path, root.parent)
    assert not (root / 'new-smoke-data').exists()
