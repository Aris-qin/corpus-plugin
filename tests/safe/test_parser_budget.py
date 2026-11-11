from corpus_core.service import Corpus


def test_small_bytes_cannot_create_unbounded_chunks(root):
    corpus = Corpus(root)
    item = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC excessive chunk count', 'content': 'SYNTHETIC\n' * 10001}], 'operation_id': 'many-lines'})['items'][0]
    job = corpus.run_one()
    assert job['state'] == 'failed' and job['error'] == 'PARSER_LIMIT'
    assert corpus.db.execute('SELECT count(*) FROM source_versions').fetchone()[0] == 1
    assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 0
    assert corpus.read('corpus://version/' + item['version_id'])['generation'] is None
