import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")
from test_retrieval import seed


def test_parent_schema_relations_and_self_written_quarantine(root):
    from pathlib import Path
    from corpus_core.migration import migrate
    root.mkdir(parents=True)
    schema = json.loads((Path(__file__).parents[2] / 'LEGACY_SCHEMA.json').read_text())
    source_path = root / 'SYNTHETIC-parent-schema.sqlite'
    with sqlite3.connect(source_path) as source:
        for name, ddl in schema['schema']:
            if name != 'sqlite_sequence' and not name.startswith('chunk_vectors'):
                source.execute(ddl)
        source.execute("INSERT INTO documents(pmid,title,source) VALUES ('synthetic-source','SYNTHETIC original','file_parse')")
        source.execute("INSERT INTO documents(pmid,title,source) VALUES ('synthetic-unresolved','SYNTHETIC unknown author','self_written')")
        source.execute("INSERT INTO chunks(chunk_id,pmid,text) VALUES ('old-source','synthetic-source','SYNTHETIC evidence_original')")
        source.execute("INSERT INTO chunks(chunk_id,pmid,text) VALUES ('old-unresolved','synthetic-unresolved','SYNTHETIC wiki_maybe_unresolved')")
        source.execute("INSERT INTO document_groups(pmid,project_slug) VALUES ('synthetic-source','stable-project')")
    corpus = Corpus(root / 'candidate')
    evidence = migrate(corpus, source_path)
    assert evidence['verified']
    assert corpus.search('evidence_original', scope='linked', context='stable-project')['hits']
    assert not corpus.search('wiki_maybe_unresolved')['hits']
    assert corpus.resolve_legacy('old-unresolved')['matches'][0]['raw_payload']


def test_apply_cannot_cross_reference_manage_domains(root):
    from corpus_core.tools import dispatch
    from test_research import draft
    corpus = Corpus(root)
    item = seed(corpus)
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_publish(revision['revision_id'], 'p')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    with pytest.raises(CorpusError, match='domain'):
        dispatch(corpus, 'corpus_manage', {'action': 'apply', 'proposal_id': proposal['proposal_id'], 'approval_ref': approval['approval_ref'], 'operation_id': 'wrong-tool'})


def test_no_network_inside_vector_write_transaction(root):
    from corpus_core.embeddings import APIEmbedding, Profile
    corpus = Corpus(root)
    seed(corpus)
    profile = Profile('explicit-real-api', 'synthetic-provider-response', 2)
    def transport(payload):
        assert not corpus.db.in_transaction
        assert payload['model'] == profile.exact_model
        return {'data': [{'index': index, 'embedding': [1.0, 0.0]} for index, text in enumerate(payload['input'])]}
    corpus.index_vectors(profile, APIEmbedding(profile, transport))
    other = Profile('explicit-real-api', 'different-model-same-dim', 2)
    assert not corpus.search('notfound', profile=other, query_vector=[1.0, 0.0])['hits']
    with pytest.raises(CorpusError):
        corpus.index_vectors(Profile('mock', 'test', 2), lambda texts: [[1, 0]], test_root=root)


def test_lost_lease_does_not_clobber_new_runner(root, monkeypatch):
    import corpus_core.service as service
    corpus = Corpus(root)
    item = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC evidence'}], 'operation_id': 'lease'})['items'][0]
    original = service.parse
    competed = False
    def interleave(*args):
        nonlocal competed
        if not competed:
            competed = True
            rival = Corpus(root)
            rival.db.execute('UPDATE jobs SET lease_until=0 WHERE id=?', (item['job_id'],))
            assert rival.run_one()['state'] == 'succeeded'
            rival.close()
        return original(*args)
    monkeypatch.setattr(service, 'parse', interleave)
    assert corpus.run_one()['state'] == 'succeeded'
    assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 1
    assert corpus.job(item['job_id'])['attempt'] == 2


def test_cancelled_expired_worker_is_reaped(root):
    corpus = Corpus(root)
    item = seed(corpus)
    corpus.db.execute("UPDATE jobs SET state='running',lease_until=0,cancel_requested=1 WHERE id=?", (item['job_id'],))
    assert corpus.run_one() is None
    assert corpus.job(item['job_id'])['state'] == 'cancelled'


def test_concurrent_object_publication_is_no_clobber(root):
    from corpus_core.objects import publish
    root.mkdir(parents=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: publish(root, b'SYNTHETIC shared bytes'), range(20)))
    assert len({row['hash'] for row in results}) == 1
    assert len(list((root / 'objects').glob('sha256/*/*'))) == 1


def test_db_failure_leaves_original_recoverable(root):
    from corpus_core.objects import fsck
    corpus = Corpus(root)
    first = {'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC first', 'identifiers': [{'scheme': 'pmid', 'raw_value': '12345', 'verification_source': 'synthetic source fixture'}]}
    corpus.ingest({'inputs': [first], 'operation_id': 'first'})
    with pytest.raises(CorpusError, match='identifier'):
        corpus.ingest({'inputs': [{**first, 'content': 'SYNTHETIC second'}], 'operation_id': 'second'})
    assert corpus.db.execute('SELECT count(*) FROM works').fetchone()[0] == 1
    assert fsck(corpus)['unreferenced_or_derived_objects']


def test_batch_pending_dependencies_retry_does_not_repeat_applied(root):
    from test_research import draft
    corpus = Corpus(root)
    item = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC pending', 'content': 'SYNTHETIC pending'}], 'operation_id': 'pending'})['items'][0]
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_batch([{'action': 'save_source', 'input': {'source': 'text', 'title': 'SYNTHETIC batch', 'content': 'SYNTHETIC batch'}}, {'action': 'publish_reference', 'revision_id': revision['revision_id']}], 'batch')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    partial = corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'partial')
    assert partial['status'] == 'partial'
    assert partial['items'][1]['state'] == 'approved_pending_dependencies'
    assert corpus.run_one()['state'] == 'succeeded'
    result = corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'retry')
    assert result['status'] == 'applied'
    assert corpus.db.execute('SELECT count(*) FROM works').fetchone()[0] == 2


def test_generation_and_research_cross_owner_constraints(root):
    from test_research import draft
    corpus = Corpus(root)
    first = seed(corpus)
    second = seed(corpus, 'SYNTHETIC second', 'second')
    generation = corpus.db.execute('SELECT generation_id FROM active_generations WHERE version_id=?', (first['version_id'],)).fetchone()[0]
    corpus.db.execute('DELETE FROM active_generations')
    with pytest.raises(sqlite3.IntegrityError):
        corpus.db.execute('INSERT INTO active_generations VALUES (?,?)', (second['version_id'], generation))
    revision = draft(corpus, 'corpus://version/' + first['version_id'])
    other = draft(corpus, 'corpus://version/' + first['version_id'], 'SYNTHETIC other research')
    with pytest.raises(sqlite3.IntegrityError):
        corpus.db.execute('UPDATE research_records SET current_revision_id=? WHERE id=?', (revision['revision_id'], other['record_id']))
