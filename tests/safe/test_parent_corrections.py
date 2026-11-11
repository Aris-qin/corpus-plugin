import json
import sqlite3
from pathlib import Path

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")
from test_retrieval import seed
from test_research import draft


def approved(corpus, proposal, operation='apply'):
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    return corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], operation)


def merge(corpus, source, target, operation='merge'):
    return corpus.propose_maintenance({'action': 'propose', 'maintenance': 'merge',
        'target': source['work_id'], 'expected_revision': 1, 'merge_into': target['work_id'],
        'merge_expected_revision': 1, 'reason': 'SYNTHETIC verified identity', 'operation_id': operation})


def test_merge_discovery_contexts_fixed_uris_and_no_chains(root):
    corpus = Corpus(root)
    source = seed(corpus, 'SYNTHETIC unique_alpha', 'a')
    target = seed(corpus, 'SYNTHETIC unique_beta', 'b')
    corpus.link(source['work_id'], 'alpha', 'link-a')
    corpus.link(target['work_id'], 'beta', 'link-b')
    old_ref = corpus.search('unique_alpha')['hits'][0]['ref']
    approved(corpus, merge(corpus, source, target))
    for context in (None, 'alpha', 'beta'):
        for query in ('unique_alpha', 'unique_beta'):
            hits = corpus.search(query, scope='linked' if context else 'global', context=context)['hits']
            assert hits and hits[0]['work_id'] == target['work_id']
    assert corpus.read(old_ref)['chunk']['text'] == 'SYNTHETIC unique_alpha'
    navigation = corpus.read('corpus://work/' + target['work_id'])
    assert {item['id'] for item in navigation['versions']} == {source['version_id'], target['version_id']}
    third = seed(corpus, 'SYNTHETIC third', 'c')
    with pytest.raises(CorpusError):
        corpus.propose_maintenance({'maintenance': 'merge', 'target': target['work_id'],
            'expected_revision': 2, 'merge_into': third['work_id'], 'merge_expected_revision': 1,
            'reason': 'SYNTHETIC', 'operation_id': 'chain'})


def test_merge_impact_changes_require_reproposal(root):
    corpus = Corpus(root)
    source = seed(corpus, 'SYNTHETIC alpha', 'a')
    target = seed(corpus, 'SYNTHETIC beta', 'b')
    proposal = merge(corpus, source, target)
    corpus.link(source['work_id'], 'new-context', 'later-link')
    with pytest.raises(CorpusError, match='impact'):
        approved(corpus, proposal)


def test_status_and_reviews_visible_on_precise_evidence(root):
    corpus = Corpus(root)
    item = seed(corpus)
    ref = 'corpus://version/' + item['version_id']
    corpus.metadata(item['work_id'], 1, {'retraction_status': 'retracted'}, ref, 'status')
    corpus.review(ref, ['retraction_status'], ref, 'SYNTHETIC checked', ['verify replacement'], 'review')
    hit = corpus.search('evidence')['hits'][0]
    for result in (hit, corpus.read(ref), corpus.read(hit['ref'], 'submission')):
        assert result['source_status']['metadata']['retraction_status']['value'] == 'retracted'
        assert 'source_retracted' in result['warnings']
        assert result['reviews'][0]['scope'] == ['retraction_status']
    assert 'verify replacement' in corpus.read(hit['ref'], 'submission')['reminders']


def test_publication_freezes_work_state(root):
    corpus = Corpus(root)
    item = seed(corpus)
    ref = 'corpus://version/' + item['version_id']
    revision = draft(corpus, ref)
    proposal = corpus.propose_publish(revision['revision_id'], 'propose')
    corpus.metadata(item['work_id'], 1, {'retraction_status': 'retracted'}, ref, 'retraction')
    with pytest.raises(CorpusError, match='dependenc'):
        approved(corpus, proposal)


def test_corrupt_retry_retains_active_generation(root):
    corpus = Corpus(root)
    item = seed(corpus)
    old = corpus.read('corpus://version/' + item['version_id'])['generation']['id']
    corpus.db.execute("UPDATE jobs SET state='queued',payload=? WHERE id=?",
        (json.dumps({'version_id': item['version_id'], 'pipeline': 'new'}), item['job_id']))
    assert corpus.run_one(fail_before_activate=True)['state'] == 'failed'
    blob = corpus.db.execute('SELECT path FROM blobs').fetchone()
    (root / blob['path']).write_bytes(b'SYNTHETIC corrupted')
    corpus.jobs_control('retry', item['job_id'], 'retry')
    result = corpus.run_one()
    assert result['state'] == 'failed' and result['error'] == 'OBJECT_CORRUPT'
    assert corpus.read('corpus://version/' + item['version_id'])['generation']['id'] == old


def test_blob_dedup_preserves_kind_and_checks_revision(root):
    corpus = Corpus(root)
    item = seed(corpus, 'SYNTHETIC identical')
    ref = 'corpus://version/' + item['version_id']
    source = {'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC identical',
              'work_id': item['work_id'], 'expected_revision': 999, 'relationship_source': ref, 'version_kind': 'formal'}
    with pytest.raises(CorpusError):
        corpus.ingest({'inputs': [source], 'operation_id': 'stale'})
    source['expected_revision'] = 1
    formal = corpus.ingest({'inputs': [source], 'operation_id': 'formal'})['items'][0]
    assert formal['version_id'] != item['version_id']
    assert corpus.read('corpus://version/' + formal['version_id'])['version']['version_kind'] == 'formal'
    assert corpus.db.execute('SELECT count(*) FROM blobs').fetchone()[0] == 1


@pytest.mark.parametrize('changes', [{'year': []}, {'year': True}, {'authors': 'not a list'},
    {'doi': 'not-doi'}, {'retraction_status': {'untrusted': True}}])
def test_strict_metadata(root, changes):
    corpus = Corpus(root)
    item = seed(corpus)
    with pytest.raises(CorpusError):
        corpus.metadata(item['work_id'], 1, changes, 'corpus://version/' + item['version_id'], 'bad')


def test_doi_metadata_updates_identity_and_rejects_conflict(root):
    corpus = Corpus(root)
    first = seed(corpus, 'SYNTHETIC first', 'first')
    second = seed(corpus, 'SYNTHETIC second', 'second')
    corpus.metadata(first['work_id'], 1, {'doi': 'https://doi.org/10.1234/ABC'},
                    'corpus://version/' + first['version_id'], 'doi')
    row = corpus.db.execute('SELECT * FROM external_identifiers').fetchone()
    assert row['lookup_key'] == '10.1234/abc'
    with pytest.raises(CorpusError):
        corpus.metadata(second['work_id'], 1, {'doi': '10.1234/abc'},
                        'corpus://version/' + second['version_id'], 'conflict')


def test_nested_draft_reuses_action_contract(root):
    corpus = Corpus(root)
    source = seed(corpus)
    payload = {'kind': 'topic_report', 'body': 'SYNTHETIC', 'evidence': ['corpus://version/' + source['version_id']],
               'structured': {}, 'change_summary': 'x' * 3000, 'reason': 'SYNTHETIC', 'operation_id': 'nested'}
    with pytest.raises(CorpusError):
        corpus.propose_batch([{'action': 'save_draft', 'draft': payload}], 'batch')


def test_formal_priority_and_research_review_navigation(root):
    corpus = Corpus(root)
    item = seed(corpus, 'SYNTHETIC evidence')
    ref = 'corpus://version/' + item['version_id']
    formal = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC formal',
        'content': 'SYNTHETIC evidence formal', 'work_id': item['work_id'], 'expected_revision': 1,
        'relationship_source': ref, 'version_kind': 'formal'}], 'operation_id': 'formal'})['items'][0]
    corpus.run_one()
    assert corpus.search('evidence')['hits'][0]['version_kind'] == 'formal'
    revision = draft(corpus, ref)
    approved(corpus, corpus.propose_publish(revision['revision_id'], 'publish-one'))
    fixed = 'corpus://reference/' + revision['record_id'] + '/revision/' + revision['revision_id']
    corpus.review(fixed, ['body'], ref, 'SYNTHETIC checked', ['remaining'], 'review')
    assert corpus.read('corpus://reference/' + revision['record_id'])['reviews']
    corpus.link(revision['record_id'], 'project', 'link')
    next_revision = corpus.save_draft({'kind': 'topic_report', 'body': 'SYNTHETIC next', 'evidence': [ref],
        'structured': {}, 'record_id': revision['record_id'], 'expected_revision': 1,
        'change_summary': 'SYNTHETIC next', 'reason': 'SYNTHETIC', 'operation_id': 'next'})
    approved(corpus, corpus.propose_publish(next_revision['revision_id'], 'publish-two'), 'apply-two')
    assert corpus.read(fixed)['publication_status'] == 'superseded_formal'
    assert revision['revision_id'] not in {row['id'] for row in corpus.overview('project')['items'][0]['drafts']}


def test_changed_snapshot_refused_without_duplicate_rows(root):
    from corpus_core.migration import migrate
    root.mkdir(parents=True)
    source = root / 'SYNTHETIC.sqlite'
    with sqlite3.connect(source) as connection:
        connection.executescript('CREATE TABLE documents(pmid TEXT,title TEXT,source TEXT);'
            'CREATE TABLE chunks(chunk_id TEXT,pmid TEXT,text TEXT); CREATE TABLE document_groups(pmid TEXT,project_slug TEXT);'
            "INSERT INTO documents VALUES ('one','SYNTHETIC','file_parse');")
    corpus = Corpus(root / 'candidate')
    migrate(corpus, source)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE documents SET title='SYNTHETIC changed'")
    with pytest.raises(CorpusError):
        migrate(corpus, source)
    assert corpus.db.execute('SELECT count(*) FROM works').fetchone()[0] == 1


def test_research_dependency_changes_reject_but_historical_citation_allowed(root):
    corpus = Corpus(root)
    source = seed(corpus)
    ref = 'corpus://version/' + source['version_id']
    first = draft(corpus, ref)
    approved(corpus, corpus.propose_publish(first['revision_id'], 'p1'), 'a1')
    first_ref = 'corpus://reference/' + first['record_id'] + '/revision/' + first['revision_id']
    dependent = draft(corpus, first_ref, 'SYNTHETIC dependent')
    stale = corpus.propose_publish(dependent['revision_id'], 'dependent-before')
    second = corpus.save_draft({'kind': 'topic_report', 'body': 'SYNTHETIC successor', 'evidence': [ref],
        'structured': {}, 'record_id': first['record_id'], 'expected_revision': 1,
        'change_summary': 'SYNTHETIC', 'reason': 'SYNTHETIC', 'operation_id': 'successor'})
    approved(corpus, corpus.propose_publish(second['revision_id'], 'p2'), 'a2')
    with pytest.raises(CorpusError, match='dependency'):
        approved(corpus, stale, 'dependent-stale')
    assert approved(corpus, corpus.propose_publish(dependent['revision_id'], 'dependent-after'), 'dependent-new')['committed']


def test_recycled_work_dedup_cannot_write(root):
    corpus = Corpus(root)
    item = seed(corpus, 'SYNTHETIC identical')
    corpus.lifecycle(item['work_id'], 'recycled', 1, 'recycle')
    with pytest.raises(CorpusError):
        corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC identical', 'title': 'SYNTHETIC',
            'work_id': item['work_id'], 'expected_revision': 2, 'relationship_source': 'corpus://version/' + item['version_id']}], 'operation_id': 'dedup-recycled'})


def test_parser_checks_consumed_bytes_not_only_precheck(root, monkeypatch):
    import corpus_core.service as service
    corpus = Corpus(root)
    corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC original', 'title': 'SYNTHETIC'}], 'operation_id': 'submit'})
    original = service.parse
    def replace_before_consumption(root, path, *args):
        path.write_bytes(b'SYNTHETIC replaced')
        return original(root, path, *args)
    monkeypatch.setattr(service, 'parse', replace_before_consumption)
    result = corpus.run_one()
    assert result['state'] == 'failed' and result['error'] == 'OBJECT_CORRUPT'


def test_merge_approval_freezes_newly_materialized_fixed_refs(root):
    corpus = Corpus(root)
    target = seed(corpus, 'SYNTHETIC target', 'target')
    source = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC pending'}], 'operation_id': 'source'})['items'][0]
    proposal = merge(corpus, source, target)
    corpus.run_one()
    with pytest.raises(CorpusError, match='impact'):
        approved(corpus, proposal)


def test_merged_original_status_filter_does_not_hide_retraction(root):
    corpus = Corpus(root)
    source = seed(corpus, 'SYNTHETIC retracted_evidence', 'source')
    target = seed(corpus, 'SYNTHETIC other', 'target')
    corpus.metadata(source['work_id'], 1, {'retraction_status': 'retracted'}, 'corpus://version/' + source['version_id'], 'retract')
    proposal = corpus.propose_maintenance({'maintenance': 'merge', 'target': source['work_id'], 'expected_revision': 2,
        'merge_into': target['work_id'], 'merge_expected_revision': 1, 'reason': 'SYNTHETIC', 'operation_id': 'merge'})
    approved(corpus, proposal)
    assert corpus.search('retracted_evidence', filters={'retraction_status': ['retracted']})['hits']
