import json

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")
from test_retrieval import seed


def draft(corpus, evidence, body='SYNTHETIC researcher judgement'):
    return corpus.save_draft({'kind': 'topic_report', 'body': body, 'evidence': [evidence],
                              'change_summary': 'test revision', 'reason': 'synthetic research',
                              'structured': {}, 'operation_id': body})


def test_frozen_publication_separate_approval_and_replay(root):
    corpus = Corpus(root)
    item = seed(corpus)
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_publish(revision['revision_id'], 'proposal')
    with pytest.raises(CorpusError):
        corpus.apply_proposal(proposal['proposal_id'], 'model-says-confirmed', 'apply')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    result = corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'apply')
    assert result['committed'] is True
    assert corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'apply') == result
    with pytest.raises(CorpusError):
        corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'replay')
    assert corpus.db.execute('SELECT count(*) FROM citations').fetchone()[0] == 1
    assert corpus.db.execute('SELECT state FROM export_outbox').fetchone()[0] == 'pending'
    stale = corpus.propose_publish(revision['revision_id'], 'stale-proposal')
    accepted = corpus.operator_approve(stale['proposal_id'], stale['hash'], stale['binding'])
    corpus.db.execute('UPDATE research_records SET revision=revision+1')
    with pytest.raises(CorpusError, match='revision'):
        corpus.apply_proposal(stale['proposal_id'], accepted['approval_ref'], 'stale-apply')


def test_guidance_and_metadata_do_not_rewrite_research(root):
    corpus = Corpus(root)
    item = seed(corpus)
    with pytest.raises(CorpusError, match='article_type'):
        corpus.save_draft({'kind': 'author_guidance', 'body': 'SYNTHETIC', 'evidence': [], 'structured': {}, 'change_summary': 'a', 'reason': 'b', 'operation_id': 'bad'})
    evidence = 'corpus://version/' + item['version_id']
    requirements = {key: {'status': 'not_provided', 'source_ref': evidence, 'locator': {}, 'checked_at': None, 'value': None} for key in ('length', 'abstract', 'sections', 'references', 'figures', 'supplements', 'declarations', 'submission_materials', 'templates')}
    revision = corpus.save_draft({'kind': 'author_guidance', 'body': 'SYNTHETIC guidance', 'evidence': [evidence], 'structured': {'article_type': 'review', 'journal_id': 'synthetic-journal', 'requirements': requirements}, 'change_summary': 'a', 'reason': 'b', 'operation_id': 'guidance'})
    corpus.review('corpus://reference/' + revision['record_id'] + '/revision/' + revision['revision_id'], ['length'], evidence, 'unchanged', ['fees'], 'review')
    for repeat in range(2):
        result = corpus.read('corpus://reference/' + revision['record_id'] + '/revision/' + revision['revision_id'], use_context='submission')
        assert result['reminders']
    assert corpus.db.execute('SELECT count(*) FROM research_revisions').fetchone()[0] == 1


def test_batch_partial_and_lifecycle_shared_blob(root):
    corpus = Corpus(root)
    item = seed(corpus)
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_batch([
        {'action': 'save_source', 'input': {'source': 'text', 'title': 'SYNTHETIC new', 'content': 'SYNTHETIC batch source'}},
        {'action': 'publish_reference', 'revision_id': revision['revision_id']},
    ], 'batch')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    result = corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'batch-apply')
    assert [entry['state'] for entry in result['items']] == ['applied', 'applied']
    corpus.lifecycle(item['work_id'], 'recycled', 1, 'recycle')
    assert corpus.read('corpus://version/' + item['version_id'])['version']
    corpus.lifecycle(item['work_id'], 'active', 2, 'restore')
    assert corpus.db.execute('SELECT lifecycle FROM works WHERE id=?', (item['work_id'],)).fetchone()[0] == 'active'
