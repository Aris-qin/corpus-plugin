import json
import time
import tracemalloc

import pytest

from corpus_core.db import encoded
from corpus_core.embeddings import Profile
from corpus_core.service import Corpus
from corpus_core.rerank import RerankRuntime
from test_rerank import config, response
from test_retrieval import seed


@pytest.fixture
def library(root):
    corpus = Corpus(root, test_mode=True)
    item = seed(corpus, 'SYNTHETIC filler')
    generation = corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0]
    corpus.db.execute('BEGIN IMMEDIATE')
    for ordinal in range(10024):
        text = 'SYNTHETIC filler'
        if ordinal >= 10020:
            text = 'SYNTHETIC abletooperateforalongperiodoftime 中文研究 token-with.punctuation'
        chunk_id = f'SYNTHETIC_{ordinal:06d}'
        corpus.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',
                          (chunk_id, generation, ordinal + 1, text, '{}'))
        corpus.db.execute('INSERT INTO chunk_fts VALUES (?,?)', (chunk_id, text))
    corpus.db.execute('COMMIT')
    yield corpus, item
    corpus.close()


@pytest.mark.parametrize('query', ['abletooperateforalongperiodoftime', '研究', 'token-with.punctuation', '"研究" + ()'])
def test_full_library_lexical_recall_with_low_budget(library, query):
    corpus, item = library
    result = corpus.search(query, candidate_budget=2)
    assert len(result['hits']) == 2
    assert all('abletooperate' in hit['text'] for hit in result['hits'])
    assert result['candidate_count'] <= 2 and result['truncated']
    assert result['retrieval_diagnostics']['eligible_chunks_scanned'] == 10025
    assert result['retrieval_diagnostics']['lexical_matches'] == 4


def test_full_library_empty_and_no_matches(library):
    corpus, item = library
    result = corpus.search('SYNTHETIC_absent', candidate_budget=1)
    assert result['hits'] == [] and not result['truncated']
    assert result['status'] == 'no_matching_results'
    empty = corpus.search('研究', candidate_budget=1, scope='linked', context='absent')
    assert empty['hits'] == [] and not empty['truncated']


def test_full_library_vector_rank_and_bounded_working_set(library):
    corpus, item = library
    profile = Profile('mock', 'SYNTHETIC-full-scan', 2)
    corpus.db.execute('INSERT INTO embedding_profiles VALUES (?,?)', (profile.id, encoded(profile.config)))
    corpus.db.executemany('INSERT INTO chunk_embeddings VALUES (?,?,?)',
                         ((f'SYNTHETIC_{ordinal:06d}', profile.id,
                           encoded([1.0, 0.0] if ordinal == 10023 else [0.0, 1.0])) for ordinal in range(10024)))
    tracemalloc.start()
    started = time.monotonic()
    result = corpus.search('absent', candidate_budget=2, profile=profile, query_vector=[1.0, 0.0])
    elapsed = time.monotonic() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    observation = {'chunks': 10024, 'budget': 2, 'seconds': elapsed, 'python_peak_bytes': peak}
    (corpus.root / 'recall-working-set.json').write_text(json.dumps(observation))
    assert result['hits'][0]['ref'].endswith('SYNTHETIC_010023')
    assert result['hits'][0]['vector_score'] == 1.0
    assert result['vector_candidates'] == 2 and result['candidate_count'] == 2
    assert result['retrieval_diagnostics']['vectors_scanned'] == 10024
    assert peak < 4 * 1024 * 1024


def test_full_library_filters_rerank_and_repeated_pages(library):
    corpus, item = library
    corpus.link(item['work_id'], 'SYNTHETIC-context', 'synthetic-link')
    calls = []
    def offline_transport(payload):
        calls.append(payload)
        return response(payload)
    corpus.rerank = RerankRuntime(corpus, config(), offline_transport)
    arguments = dict(candidate_budget=3, top_k=1, scope='linked', context='SYNTHETIC-context',
                     filters={'version_ids': [item['version_id']]})
    first = corpus.search('研究', **arguments)
    assert first['hits'] and first['cursor']
    second = corpus.search('研究', cursor=first['cursor'], **arguments)
    assert first['hits'][0]['ref'] != second['hits'][0]['ref']
    assert second['hits'] == corpus.search('研究', cursor=first['cursor'], **arguments)['hits']
    assert len(calls) == 1 and len(calls[0]['input']['documents']) == 3
    assert all('abletooperate' in text for text in calls[0]['input']['documents'])
    corpus.lifecycle(item['work_id'], 'recycled', 1, 'synthetic-recycle')
    assert not corpus.search('研究', candidate_budget=1)['hits']


def test_full_library_version_preference_and_merged_identity(library):
    corpus, item = library
    formal = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC formal',
        'content': 'SYNTHETIC 中文研究', 'work_id': item['work_id'], 'expected_revision': 1,
        'version_kind': 'formal', 'relationship_source': 'corpus://version/' + item['version_id']}],
        'operation_id': 'synthetic-formal'})['items'][0]
    assert corpus.run_one()['state'] == 'succeeded'
    assert corpus.search('研究', candidate_budget=1)['hits'][0]['version_id'] == formal['version_id']
    canonical = seed(corpus, 'SYNTHETIC canonical', 'synthetic-canonical')
    corpus.link(item['work_id'], 'SYNTHETIC-alias-context', 'synthetic-alias-link')
    corpus.db.execute("UPDATE works SET metadata=?,lifecycle='recycled' WHERE id=?",
                      (encoded({'merged_into': canonical['work_id']}), item['work_id']))
    arguments = dict(candidate_budget=2, scope='linked', context='SYNTHETIC-alias-context',
                     filters={'work_ids': [canonical['work_id']], 'version_ids': [item['version_id']]})
    hits = corpus.search('研究', **arguments)['hits']
    assert len(hits) == 2
    assert all(hit['work_id'] == canonical['work_id'] and hit['original_work_id'] == item['work_id'] for hit in hits)
    corpus.lifecycle(canonical['work_id'], 'recycled', 1, 'synthetic-recycle-canonical')
    assert not corpus.search('研究', **arguments)['hits']
