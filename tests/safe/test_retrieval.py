import json

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


def seed(corpus, content='SYNTHETIC 中文研究 evidence', operation='seed'):
    item = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC source', 'content': content}], 'operation_id': operation})['items'][0]
    assert corpus.run_one()['state'] == 'succeeded'
    return item


def test_global_linked_chinese_and_fixed_citations(root):
    corpus = Corpus(root)
    item = seed(corpus)
    corpus.link(item['work_id'], 'a', 'link-a')
    assert corpus.search('研究', context='b')['hits']
    assert not corpus.search('研究', scope='linked', context='b')['hits']
    hit = corpus.search('研究')['hits'][0]
    fixed = corpus.read(hit['ref'])
    corpus.db.execute('UPDATE jobs SET state=\'queued\',payload=? WHERE id=?', (json.dumps({'version_id': item['version_id'], 'pipeline': 'new'}), item['job_id']))
    assert corpus.run_one(fail_before_activate=True)['state'] == 'failed'
    assert corpus.read(hit['ref']) == fixed
    assert corpus.search('研究')['hits'][0]['ref'] == hit['ref']


def test_bound_cursor_and_exact_vector_filter(root):
    from corpus_core.embeddings import Profile
    corpus = Corpus(root, test_mode=True)
    seed(corpus, 'SYNTHETIC evidence one\nevidence two\nevidence three')
    first = corpus.search('evidence', top_k=1)
    assert first['cursor']
    second = corpus.search('evidence', top_k=1, cursor=first['cursor'])
    assert first['hits'][0]['ref'] != second['hits'][0]['ref']
    with pytest.raises(CorpusError, match='cursor'):
        corpus.search('other', top_k=1, cursor=first['cursor'])
    with pytest.raises(CorpusError):
        Profile('mock', 'synthetic', 2).validate(test_root=None)
    profile = Profile('mock', 'synthetic', 2)
    corpus.index_vectors(profile, lambda texts: [[1.0, 0.0] for text in texts], test_root=root)
    result = corpus.search('notlexical', profile=profile, query_vector=[1.0, 0.0])
    assert result['hits'] and result['vector_candidates'] == 3
    assert corpus.search('notlexical', profile=profile, query_vector=[1.0, 0.0], scope='linked', context='absent')['hits'] == []
