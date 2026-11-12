import heapq
import sys

import pytest

from corpus_core.errors import CorpusError
from corpus_core.paging import page
from test_candidate_budget import library
from test_retrieval import seed


def test_rrf_uses_retained_ranks_only(root, monkeypatch):
    corpus, profile = library(root, 12, vectors=True)
    extra = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC formal',
        'content': 'needle ' + 'extra ' * 50, 'version_kind': 'formal'}],
        'operation_id': 'formal'})['items'][0]
    assert corpus.run_one()['state'] == 'succeeded'
    retained_lists = []
    original = heapq.nsmallest

    def recording(*args, **kwargs):
        retained = original(*args, **kwargs)
        retained_lists.append(sorted(retained, key=lambda item: (-item[1], item[0])))
        return retained

    monkeypatch.setattr(heapq, 'nsmallest', recording)
    result = corpus.search('needle', candidate_budget=3, profile=profile, query_vector=[1.0, 0.0])
    expected = {}
    for ranking in retained_lists:
        for rank, item in enumerate(ranking, 1):
            expected[item[0]] = expected.get(item[0], 0) + 1 / (60 + rank)
    formal_ids = {row[0] for row in corpus.db.execute('SELECT c.id FROM chunks c JOIN generations g ON g.id=c.generation_id WHERE g.version_id=?', (extra['version_id'],))}
    order = sorted(expected, key=lambda key: (key not in formal_ids, -expected[key], key))[:3]
    assert [hit['ref'].split('/')[-1] for hit in result['hits']] == order
    assert [hit['rrf_score'] for hit in result['hits']] == [expected[key] for key in order]
    corpus.close()


def test_complete_result_not_marked_truncated_at_scale(root):
    corpus, profile = library(root, 40001, vectors=True)
    result = corpus.search('needle', candidate_budget=10000, profile=profile, query_vector=[1.0, 0.0])
    assert result['status'] == 'matching_results' and result['truncated'] is False
    assert result['retrieval_diagnostics']['fusion_policy'] == 'retained_rank_v1'
    corpus.close()


def test_list_cut_upper_bound_triggers_truncation(root):
    corpus, _ = library(root, 1000)
    result = corpus.search('filler', candidate_budget=2)
    assert result['truncated'] is True
    assert result['retrieval_diagnostics']['list_cut_upper_bound'] == 2 / 62
    corpus.close()


@pytest.mark.parametrize('overlap', [False, True])
def test_union_cut_uses_exact_scores(root, overlap):
    corpus, profile = library(root, 1, vectors=True)
    result = corpus.search('needle' if overlap else 'SYNTHETIC', candidate_budget=1,
                           profile=profile, query_vector=[1.0, 0.0])
    diagnostics = result['retrieval_diagnostics']
    assert diagnostics['list_cut_upper_bound'] is None
    assert result['truncated'] is (not overlap)
    assert diagnostics['union_cut'] is (not overlap)
    if not overlap:
        assert diagnostics['best_discarded_upper_bound'] == diagnostics['page_floor_fused_score']
    assert 'best_discarded_fused_score' not in diagnostics
    corpus.close()


def test_fused_metadata_is_bounded(root):
    corpus, profile = library(root, 150, vectors=True)
    peak = 0

    def observe(frame, event, arg):
        nonlocal peak
        if frame.f_code.co_filename.endswith('/corpus_core/retrieval.py'):
            for name in ('full_fused', 'combined'):
                value = frame.f_locals.get(name)
                if isinstance(value, dict):
                    peak = max(peak, len(value))
        return observe

    sys.settrace(observe)
    try:
        corpus.search('filler', candidate_budget=3, profile=profile, query_vector=[1.0, 0.0])
    finally:
        sys.settrace(None)
        corpus.close()
    print(f'fusion_peak_entries={peak}; budget=3; candidates=150')
    assert peak <= 6


def test_cursor_rejects_pre_rollback_binding(root):
    corpus, _ = library(root, 4)
    binding = {'query': 'needle', 'scope': 'global', 'context': None, 'top_k': 1,
               'profile': None, 'query_vector': None, 'budget': 10000, 'filters': {},
               'best_discarded_fused_score': None}
    _, cursor = page(root, corpus.search('needle')['hits'], binding, 1, None)
    with pytest.raises(CorpusError):
        corpus.search('needle', top_k=1, cursor=cursor)
    corpus.close()


def test_order_deterministic_across_runs(root):
    corpus, profile = library(root, 12, vectors=True)
    results = [corpus.search('needle', candidate_budget=3, profile=profile, query_vector=[1.0, 0.0]) for _ in range(3)]
    assert all(result['hits'] == results[0]['hits'] for result in results)
    assert results[0]['retrieval_diagnostics']['fusion_policy'] == 'retained_rank_v1'
    corpus.close()


def test_union_cut_lower_exact_score_is_complete(root):
    corpus, profile = library(root, 3, vectors=True, texts=['needle', 'needle', 'filler'])
    corpus.db.execute("DELETE FROM chunk_embeddings WHERE chunk_id='budget_000001'")
    result = corpus.search('needle', candidate_budget=2, top_k=1,
                           profile=profile, query_vector=[1.0, 0.0])
    diagnostics = result['retrieval_diagnostics']
    assert diagnostics['union_cut'] is True
    assert diagnostics['list_cut_upper_bound'] is None
    assert diagnostics['best_discarded_upper_bound'] < diagnostics['page_floor_fused_score']
    assert result['truncated'] is False
    corpus.close()
