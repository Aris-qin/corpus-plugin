import json

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")
from test_parent_corrections import approved, merge
from test_research import draft
from test_retrieval import seed


def fixed(revision):
    return 'corpus://reference/' + revision['record_id'] + '/revision/' + revision['revision_id']


@pytest.fixture
def corpus(root):
    instance = Corpus(root)
    yield instance
    instance.close()


@pytest.mark.parametrize('chunk', [False, True])
def test_transitive_status_change_requires_new_approval(corpus, chunk):
    source = seed(corpus)
    version = 'corpus://version/' + source['version_id']
    ref = corpus.search('evidence')['hits'][0]['ref'] if chunk else version
    parent = draft(corpus, ref, 'parent')
    approved(corpus, corpus.propose_publish(parent['revision_id'], 'parent-proposal'), 'parent-apply')
    child = draft(corpus, fixed(parent), 'child')
    proposal = corpus.propose_publish(child['revision_id'], 'old-proposal')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    corpus.metadata(source['work_id'], 1, {'retraction_status': 'retracted'}, version, 'retract')
    corpus.lifecycle(source['work_id'], 'recycled', 2, 'recycle')
    with pytest.raises(CorpusError, match='dependenc'):
        corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'old-apply')
    assert approved(corpus, corpus.propose_publish(child['revision_id'], 'new-proposal'), 'new-apply')['committed']
    result = corpus.read(fixed(child), 'submission')
    assert {'source_retracted', 'source_recycled'} <= set(result['warnings'])
    assert any(item.get('source_status', {}).get('id') == source['work_id'] for item in result['upstream_evidence'])


@pytest.mark.parametrize('target_kind', ['version', 'chunk', 'research'])
@pytest.mark.parametrize('nested', [False, True])
def test_new_review_invalidates_approval_but_can_be_explicitly_accepted(corpus, target_kind, nested):
    source = seed(corpus)
    version = 'corpus://version/' + source['version_id']
    chunk = corpus.search('evidence')['hits'][0]['ref']
    parent = draft(corpus, chunk, 'parent')
    approved(corpus, corpus.propose_publish(parent['revision_id'], 'parent-proposal'), 'parent-apply')
    child = draft(corpus, fixed(parent) if nested or target_kind == 'research' else chunk, 'child')
    proposal = corpus.propose_publish(child['revision_id'], 'old-proposal')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    target = {'version': version, 'chunk': chunk, 'research': fixed(parent)}[target_kind]
    corpus.review(target, ['retraction_status'], version, 'SYNTHETIC conflict', ['CONFLICT_UNRESOLVED'], 'review')
    with pytest.raises(CorpusError, match='dependenc'):
        corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'old-apply')
    assert approved(corpus, corpus.propose_publish(child['revision_id'], 'new-proposal'), 'new-apply')['committed']
    assert 'CONFLICT_UNRESOLVED' in corpus.read(fixed(child), 'submission')['reminders']


def seed_doi(corpus):
    item = corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC alias', 'title': 'alias',
        'identifiers': [{'scheme': 'doi', 'raw_value': '10.1234/A', 'verification_source': 'SYNTHETIC'}]}],
        'operation_id': 'alias'})['items'][0]
    assert corpus.run_one()['state'] == 'succeeded'
    return item


@pytest.mark.parametrize('ingest', [False, True])
def test_alias_doi_allowed_only_within_canonical_work(corpus, ingest):
    source = seed_doi(corpus)
    target = seed(corpus, 'SYNTHETIC target', 'target')
    old_chunk = corpus.search('alias')['hits'][0]['ref']
    before = corpus.read(old_chunk)['chunk']
    approved(corpus, merge(corpus, source, target), 'merge-apply')
    version = 'corpus://version/' + source['version_id']
    if ingest:
        result = corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC new version', 'title': 'target',
            'work_id': target['work_id'], 'expected_revision': 2, 'relationship_source': version,
            'identifiers': [{'scheme': 'doi', 'raw_value': 'https://doi.org/10.1234/a', 'verification_source': version}]}],
            'operation_id': 'new-version'})
        assert result['items'][0]['work_id'] == target['work_id']
    else:
        assert corpus.metadata(target['work_id'], 2, {'doi': '10.1234/a'}, version, 'doi')['revision'] == 3
    assert corpus.read(old_chunk)['chunk'] == before
    alias_identifier = corpus.db.execute('SELECT * FROM external_identifiers WHERE owner_id=?', (source['work_id'],)).fetchone()
    assert alias_identifier['raw_value'] == '10.1234/A'
    assert alias_identifier['verification_source'] == 'SYNTHETIC'
    other = seed(corpus, 'SYNTHETIC unrelated', 'other')
    with pytest.raises(CorpusError, match='another work'):
        corpus.metadata(other['work_id'], 1, {'doi': '10.1234/a'}, version, 'collision')
    with pytest.raises(CorpusError, match='another work'):
        corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC unrelated collision', 'title': 'other',
            'identifiers': [{'scheme': 'doi', 'raw_value': '10.1234/a', 'verification_source': version}]}],
            'operation_id': 'ingest-collision'})


def test_transitive_canonical_status_change_invalidates_approval(corpus):
    source = seed(corpus)
    target = seed(corpus, 'SYNTHETIC canonical', 'canonical')
    approved(corpus, merge(corpus, source, target), 'merge-apply')
    version = 'corpus://version/' + source['version_id']
    parent = draft(corpus, version, 'parent')
    approved(corpus, corpus.propose_publish(parent['revision_id'], 'parent-proposal'), 'parent-apply')
    middle = draft(corpus, fixed(parent), 'middle')
    approved(corpus, corpus.propose_publish(middle['revision_id'], 'middle-proposal'), 'middle-apply')
    child = draft(corpus, fixed(middle), 'child')
    proposal = corpus.propose_publish(child['revision_id'], 'old-proposal')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    corpus.metadata(target['work_id'], 2, {'retraction_status': 'retracted'}, version, 'retract-canonical')
    with pytest.raises(CorpusError, match='dependency'):
        corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'old-apply')
    assert 'source_retracted' in corpus.read(fixed(child), 'submission')['warnings']


def test_unrelated_review_does_not_invalidate_approval(corpus):
    source = seed(corpus)
    other = seed(corpus, 'SYNTHETIC unrelated', 'other')
    version = 'corpus://version/' + source['version_id']
    child = draft(corpus, version, 'child')
    proposal = corpus.propose_publish(child['revision_id'], 'proposal')
    corpus.review('corpus://version/' + other['version_id'], ['title'], version, 'SYNTHETIC', ['UNRELATED'], 'review')
    assert approved(corpus, proposal)['committed']


def test_apply_recomputes_bounded_closure_without_consuming_approval(corpus, monkeypatch):
    import corpus_core.research as research
    source = seed(corpus)
    parent = draft(corpus, 'corpus://version/' + source['version_id'], 'parent')
    approved(corpus, corpus.propose_publish(parent['revision_id'], 'parent-proposal'), 'parent-apply')
    child = draft(corpus, fixed(parent), 'child')
    proposal = corpus.propose_publish(child['revision_id'], 'child-proposal')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    with monkeypatch.context() as patched:
        patched.setattr(research, 'MAX_EVIDENCE_NODES', 1)
        with pytest.raises(CorpusError, match='evidence'):
            corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'bounded-apply')
    assert corpus.db.execute('SELECT consumed FROM approvals WHERE id=?', (approval['approval_ref'],)).fetchone()[0] == 0
    assert corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'retry-apply')['committed']


def test_unchanged_metadata_preserves_recorded_date(corpus, monkeypatch):
    import corpus_core.management as management
    source = seed(corpus)
    version = 'corpus://version/' + source['version_id']
    monkeypatch.setattr(management, 'now', lambda: '2026-11-01T00:00:00Z')
    corpus.metadata(source['work_id'], 1, {'retraction_status': 'retracted'}, version, 'first')
    monkeypatch.setattr(management, 'now', lambda: '2026-11-10T00:00:00Z')
    corpus.metadata(source['work_id'], 2, {'retraction_status': 'retracted', 'title': 'SYNTHETIC new title'}, version, 'second')
    metadata = json.loads(corpus.db.execute('SELECT metadata FROM works WHERE id=?', (source['work_id'],)).fetchone()[0])
    assert metadata['retraction_status']['at'] == '2026-11-01T00:00:00Z'


def test_closure_deduplicates_shared_upstream_evidence(corpus):
    source = seed(corpus)
    version = 'corpus://version/' + source['version_id']
    parent = draft(corpus, version, 'parent')
    approved(corpus, corpus.propose_publish(parent['revision_id'], 'parent-proposal'), 'parent-apply')
    child = corpus.save_draft({'kind': 'topic_report', 'body': 'child', 'evidence': [fixed(parent), version, fixed(parent)],
        'structured': {}, 'change_summary': 'SYNTHETIC', 'reason': 'SYNTHETIC', 'operation_id': 'child'})
    dependencies = corpus.propose_publish(child['revision_id'], 'child-proposal')['payload']['dependencies']
    assert {item['ref'] for item in dependencies} == {fixed(parent), version}
    assert len(dependencies) == 2


@pytest.mark.parametrize('limit', ['cycle', 'depth', 'nodes'])
def test_closure_bounds_fail_closed(corpus, monkeypatch, limit):
    import corpus_core.research as research
    source = seed(corpus)
    version = 'corpus://version/' + source['version_id']
    parent = draft(corpus, version, 'parent')
    child = draft(corpus, fixed(parent), 'child')
    if limit == 'cycle':
        cycle_id = 'revision_synthetic_cycle'
        cycle_ref = 'corpus://reference/' + parent['record_id'] + '/revision/' + cycle_id
        corpus.db.execute('INSERT INTO research_revisions SELECT ?,record_id,parent_id,body,hash,?,structured,change_summary,reason,created_at FROM research_revisions WHERE id=?',
                         (cycle_id, json.dumps([cycle_ref]), parent['revision_id']))
        child = {'revision_id': cycle_id}
    elif limit == 'depth':
        monkeypatch.setattr(research, 'MAX_EVIDENCE_DEPTH', 1, raising=False)
    else:
        monkeypatch.setattr(research, 'MAX_EVIDENCE_NODES', 1, raising=False)
    with pytest.raises(CorpusError, match='evidence'):
        corpus.propose_publish(child['revision_id'], 'bounded')
