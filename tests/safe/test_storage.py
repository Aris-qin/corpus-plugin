import hashlib

import pytest

from corpus_core.errors import CorpusError


def test_originals_versions_idempotency_and_links(root):
    from corpus_core.service import Corpus
    corpus = Corpus(root)
    payload = {'inputs': [{'source': 'text', 'content': 'SYNTHETIC 中文研究资料', 'title': 'test', 'media_type': 'text/plain'}], 'operation_id': 'save1'}
    first = corpus.ingest(payload)
    assert corpus.ingest(payload) == first
    with pytest.raises(CorpusError, match='different input'):
        corpus.ingest({**payload, 'inputs': [{**payload['inputs'][0], 'content': 'changed'}]})
    version = first['items'][0]['version_id']
    work = first['items'][0]['work_id']
    corpus.link(work, 'project-a', 'a')
    corpus.link(work, 'project-b', 'b')
    assert corpus.db.execute('SELECT count(*) FROM works').fetchone()[0] == 1
    assert corpus.db.execute('SELECT count(*) FROM context_links').fetchone()[0] == 2
    row = corpus.db.execute('SELECT * FROM blobs').fetchone()
    data = (root / row['path']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == row['hash']
    assert corpus.read('corpus://version/' + version)['version']['id'] == version
    with pytest.raises(Exception):
        corpus.db.execute('UPDATE source_versions SET coverage="fulltext"')


def test_parse_failure_preserves_original_and_retry(root):
    from corpus_core.service import Corpus
    corpus = Corpus(root)
    result = corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC not pdf', 'title': 'bad pdf', 'media_type': 'application/pdf'}], 'operation_id': 'pdf'})
    job = result['items'][0]['job_id']
    assert corpus.run_one()['state'] == 'failed'
    assert corpus.db.execute('SELECT count(*) FROM version_assets').fetchone()[0] == 1
    assert corpus.job(job)['error']
