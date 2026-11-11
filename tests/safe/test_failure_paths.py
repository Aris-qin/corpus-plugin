import json
import sqlite3

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")
from test_retrieval import seed


def test_expired_and_mismatched_approvals_fail_closed(root, monkeypatch):
    import corpus_core.research as research
    from test_research import draft
    corpus = Corpus(root)
    item = seed(corpus)
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_publish(revision['revision_id'], 'p')
    with pytest.raises(CorpusError):
        corpus.operator_approve(proposal['proposal_id'], 'wrong-hash', proposal['binding'])
    with pytest.raises(CorpusError):
        corpus.operator_approve(proposal['proposal_id'], proposal['hash'], 'wrong-revision-binding')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    with pytest.raises(sqlite3.IntegrityError):
        corpus.db.execute('UPDATE proposals SET payload=?', ('{}',))
    future = research.time.time() + 90000
    monkeypatch.setattr(research.time, 'time', lambda: future)
    with pytest.raises(CorpusError):
        corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'expired')
    assert corpus.db.execute('SELECT current_revision_id FROM research_records').fetchone()[0] is None


def test_response_loss_retry_and_cancel_recover_after_restart(root):
    corpus = Corpus(root)
    payload = {'inputs': [{'source': 'text', 'title': 'SYNTHETIC restart', 'content': 'SYNTHETIC restart evidence'}], 'operation_id': 'save'}
    result = corpus.ingest(payload)
    job_id = result['items'][0]['job_id']
    corpus.close()
    corpus = Corpus(root)
    assert corpus.ingest(payload) == result
    assert corpus.jobs_control('cancel', job_id, 'cancel')['state'] == 'cancelled'
    assert corpus.run_one() is None
    assert corpus.jobs_control('retry', job_id, 'retry')['state'] == 'queued'
    assert corpus.run_one()['state'] == 'succeeded'
    assert corpus.db.execute('SELECT count(*) FROM works').fetchone()[0] == 1


def test_cursor_tamper_snapshot_change_expiry_and_bounds(root, monkeypatch):
    import corpus_core.paging as paging
    corpus = Corpus(root)
    seed(corpus, 'SYNTHETIC evidence a\nevidence b\nevidence c')
    first = corpus.search('evidence', top_k=1)
    with pytest.raises(CorpusError):
        corpus.search('evidence', top_k=1, cursor=first['cursor'] + 'x')
    future = paging.time.time() + 1000
    with monkeypatch.context() as patch:
        patch.setattr(paging.time, 'time', lambda: future)
        with pytest.raises(CorpusError, match='expired'):
            corpus.search('evidence', top_k=1, cursor=first['cursor'])
    seed(corpus, 'SYNTHETIC evidence d', 'new-source')
    with pytest.raises(CorpusError, match='snapshot'):
        corpus.search('evidence', top_k=1, cursor=first['cursor'])
    with pytest.raises(CorpusError):
        corpus.search('evidence', top_k=True)
    assert corpus.search('evidence', candidate_budget=1)['truncated']


def test_merge_approval_preserves_old_chunks_and_shared_objects(root):
    corpus = Corpus(root)
    first = seed(corpus)
    second = seed(corpus, 'SYNTHETIC second evidence', 'second')
    old_chunk = corpus.search('evidence')['hits'][0]['ref']
    proposal = corpus.propose_maintenance({'maintenance': 'merge', 'target': first['work_id'], 'expected_revision': 1,
                                           'merge_into': second['work_id'], 'merge_expected_revision': 1,
                                           'reason': 'SYNTHETIC ambiguous identity operator decision', 'operation_id': 'merge'})
    with pytest.raises(CorpusError):
        corpus.apply_proposal(proposal['proposal_id'], 'fake', 'apply')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    result = corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'apply')
    assert result['blobs_deleted'] == 0 and corpus.read(old_chunk)['chunk']
    assert all((root / row['path']).exists() for row in corpus.db.execute('SELECT path FROM blobs'))


def test_http_real_api_transport_offline_and_malformed_vectors(root):
    from corpus_core.embeddings import APIEmbedding, JSONAPITransport, Profile
    from corpus_core.network import request
    with pytest.raises(CorpusError, match='offline'):
        request('https://example.org/', 'example.org', '93.184.216.34', 100)
    profile = Profile('explicit-provider', 'SYNTHETIC', 2)
    for vector in ([float('nan'), 1.0], [0.0, 0.0], [1.0], [True, 1]):
        with pytest.raises(CorpusError):
            profile.vector(vector)
    with pytest.raises(CorpusError):
        APIEmbedding(profile, lambda payload: {'data': []})(['SYNTHETIC'])
    corpus = Corpus(root)
    seed(corpus)
    def fails(texts):
        assert not corpus.db.in_transaction
        raise TimeoutError('SYNTHETIC timeout')
    with pytest.raises(TimeoutError):
        corpus.index_vectors(profile, fails)
    assert corpus.search('evidence')['hits']
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 0


def test_disk_failure_before_db_keeps_prior_data(root, monkeypatch):
    import corpus_core.objects as objects
    corpus = Corpus(root)
    seed(corpus)
    def disk_full(descriptor):
        raise OSError('SYNTHETIC disk full')
    monkeypatch.setattr(objects.os, 'fsync', disk_full)
    with pytest.raises(OSError):
        corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC fail', 'content': 'SYNTHETIC failed data'}], 'operation_id': 'failed'})
    assert corpus.db.execute('SELECT count(*) FROM source_versions').fetchone()[0] == 1
    assert corpus.search('evidence')['hits']


def test_export_includes_current_derivative_and_detects_drift(root):
    from test_research import draft
    corpus = Corpus(root)
    item = seed(corpus)
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_publish(revision['revision_id'], 'p')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'a')
    assert corpus.export_one()['state'] == 'succeeded'
    current = root / 'reference-exports' / revision['record_id'] / 'current.md'
    assert revision['revision_id'] in current.read_text()
    current.write_text('SYNTHETIC manual edit must not be imported or overwritten')
    corpus.db.execute("UPDATE export_outbox SET state='pending'")
    assert corpus.export_one()['state'] == 'failed'
    assert current.read_text().startswith('SYNTHETIC manual')
