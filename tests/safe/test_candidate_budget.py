import pytest

from corpus_core.db import encoded
from corpus_core.embeddings import Profile
from corpus_core.service import Corpus
from test_retrieval import seed


def library(root, count, vectors=False, texts=None):
    corpus = Corpus(root, test_mode=True)
    seed(corpus, 'SYNTHETIC filler')
    generation = corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0]
    profile = Profile('mock', 'SYNTHETIC-budget', 2)
    with corpus.db:
        corpus.db.execute('BEGIN')
        if vectors:
            corpus.db.execute('INSERT INTO embedding_profiles VALUES (?,?)', (profile.id, encoded(profile.config)))
        for ordinal in range(count):
            chunk_id = f'budget_{ordinal:06d}'
            text = texts[ordinal] if texts else ('needle' if ordinal < 4 else 'filler')
            corpus.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)', (chunk_id, generation, ordinal + 1, text, '{}'))
            corpus.db.execute('INSERT INTO chunk_fts VALUES (?,?)', (chunk_id, text))
            if vectors:
                corpus.db.execute('INSERT INTO chunk_embeddings VALUES (?,?,?)',
                                  (chunk_id, profile.id, encoded([1.0, ordinal / count])))
    return corpus, profile


def test_production_shape_scan_is_not_truncation(root):
    corpus, profile = library(root, 40001, vectors=True)
    result = corpus.search('needle', profile=profile, query_vector=[1.0, 0.0])
    assert result['retrieval_diagnostics']['vectors_scanned'] == 40001
    assert result['retrieval_diagnostics']['eligible_chunks_scanned'] == 40002
    assert result['status'] == 'matching_results'
    assert result['truncated'] is False
    assert result['retrieval_diagnostics']['result_complete_for_page'] is True
    corpus.close()


def test_large_lexical_count_complete_page(root):
    corpus, _ = library(root, 10001)
    result = corpus.search('filler', candidate_budget=2, top_k=1)
    assert result['retrieval_diagnostics']['lexical_matches'] > 2
    assert result['status'] == 'candidate_budget_exhausted'
    assert result['truncated'] is True
    assert result['retrieval_diagnostics']['lexical_list_cut'] is True
    corpus.close()


def test_union_cut_equality_is_truncated(root):
    corpus, profile = library(root, 2, vectors=True)
    corpus.db.execute('UPDATE chunk_embeddings SET vector=? WHERE chunk_id=?', (encoded([0.0, 1.0]), 'budget_000000'))
    result = corpus.search('filler', candidate_budget=1, top_k=1, profile=profile, query_vector=[1.0, 0.0])
    diagnostics = result['retrieval_diagnostics']
    assert diagnostics['union_cut'] is True
    assert diagnostics['best_discarded_upper_bound'] == 2 / 61
    assert diagnostics['best_discarded_upper_bound'] >= diagnostics['page_floor_fused_score']
    assert result['truncated'] is True
    assert result['status'] == 'candidate_budget_exhausted'
    corpus.close()


def test_cross_list_discard_can_beat_page_floor(root):
    corpus, profile = library(root, 4, vectors=True,
                              texts=['needle', 'needle ' + 'extra ' * 30,
                                     'needle extra', 'needle ' + 'extra ' * 10])
    corpus.db.executemany('UPDATE chunk_embeddings SET vector=? WHERE chunk_id=?',
                         [(encoded(vector), f'budget_{ordinal:06d}') for ordinal, vector in
                          enumerate(([0.0, 1.0], [1.0, 0.0], [1.0, 0.1], [1.0, 0.2]))])
    result = corpus.search('needle', candidate_budget=1, profile=profile, query_vector=[1.0, 0.0])
    assert result['truncated'] is True
    assert result['status'] == 'candidate_budget_exhausted'
    diagnostics = result['retrieval_diagnostics']
    assert diagnostics['best_discarded_upper_bound'] > diagnostics['page_floor_fused_score']
    corpus.close()


def test_complete_cursor_does_not_duplicate_or_drop(root):
    corpus, profile = library(root, 20, vectors=True)
    arguments = dict(candidate_budget=4, top_k=1, profile=profile, query_vector=[1.0, 0.0])
    refs = []
    cursor = None
    while True:
        result = corpus.search('needle', cursor=cursor, **arguments)
        assert result['truncated'] is (len(refs) == 3)
        assert result['status'] == ('candidate_budget_exhausted' if len(refs) == 3 else 'matching_results')
        refs.extend(hit['ref'] for hit in result['hits'])
        cursor = result['cursor']
        if cursor is None:
            break
    assert refs == ['corpus://chunk/budget_' + f'{ordinal:06d}' for ordinal in range(4)]
    assert len(refs) == len(set(refs))
    corpus.close()


@pytest.mark.parametrize('top_k', [1, 2])
def test_lexical_tail_below_page_is_complete(root, top_k):
    corpus, _ = library(root, 8)
    result = corpus.search('needle', candidate_budget=2, top_k=top_k)
    assert result['truncated'] is True
    assert result['retrieval_diagnostics']['best_discarded_upper_bound'] >= result['retrieval_diagnostics']['page_floor_fused_score']
    corpus.close()
