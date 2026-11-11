import json

from corpus_core.plugin import handler_for
from corpus_core.service import Corpus
import pytest


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")


def test_every_tool_action_business_roundtrip(root, monkeypatch):
    monkeypatch.setenv('CORPUS_SANDBOX_ROOT', str(root))
    visited = set()
    def call(tool, payload):
        import jsonschema
        from corpus_core.contracts import schema_for
        jsonschema.validate(payload, schema_for(tool)['parameters'])
        result = json.loads(handler_for(tool)(payload))
        assert result['ok'], result
        visited.add((tool, payload.get('action', payload.get('mode', payload.get('view')))))
        return result['result']
    source = {'source': 'text', 'title': 'SYNTHETIC roundtrip', 'content': '# Heading\nSYNTHETIC evidence one\nevidence two'}
    assert call('corpus_ingest', {'action': 'preflight', 'inputs': [source]})['cost'] == 0
    item = call('corpus_ingest', {'action': 'submit', 'inputs': [source], 'operation_id': 'submit'})['items'][0]
    corpus = Corpus(root)
    assert corpus.run_one()['state'] == 'succeeded'
    evidence = 'corpus://version/' + item['version_id']
    chunk = call('corpus_search', {'mode': 'evidence', 'query': 'evidence'})['hits'][0]['ref']
    call('corpus_search', {'mode': 'similar', 'ref': chunk})
    call('corpus_read', {'view': 'item', 'ref': 'corpus://work/' + item['work_id']})
    call('corpus_read', {'view': 'version', 'ref': evidence})
    call('corpus_read', {'view': 'chunk', 'ref': chunk})
    call('corpus_read', {'view': 'history', 'ref': 'corpus://work/' + item['work_id']})
    call('corpus_read', {'view': 'resolve', 'ref': evidence})
    call('corpus_manage', {'action': 'metadata', 'target': item['work_id'], 'expected_revision': 1, 'changes': {'year': 2026}, 'source_ref': evidence, 'operation_id': 'metadata'})
    call('corpus_manage', {'action': 'assess', 'target': item['work_id'], 'expected_revision': 2, 'assessment': {'uncertainty': 'SYNTHETIC assessment not an approval'}, 'source_ref': evidence, 'operation_id': 'assess'})
    call('corpus_manage', {'action': 'link', 'target': item['work_id'], 'context': 'synthetic-topic', 'operation_id': 'link'})
    call('corpus_manage', {'action': 'external_link', 'target': evidence, 'uri': 'fact:synthetic-reference', 'basis': 'synthetic', 'operation_id': 'external'})
    call('corpus_manage', {'action': 'review', 'target': evidence, 'scope': ['title'], 'source_ref': evidence, 'conclusion': 'unchanged', 'remaining': ['figures'], 'operation_id': 'review'})
    call('corpus_manage', {'action': 'recycle', 'target': item['work_id'], 'expected_revision': 3, 'operation_id': 'recycle'})
    call('corpus_manage', {'action': 'restore', 'target': item['work_id'], 'expected_revision': 4, 'operation_id': 'restore'})
    draft_payload = {'action': 'save_draft', 'kind': 'topic_report', 'body': 'SYNTHETIC researcher authored', 'evidence': [evidence], 'structured': {}, 'change_summary': 'synthetic', 'reason': 'synthetic', 'operation_id': 'draft'}
    draft = call('corpus_reference', draft_payload)
    call('corpus_manage', {'action': 'link', 'target': draft['record_id'], 'context': 'synthetic-topic', 'operation_id': 'link-reference'})
    assert call('corpus_search', {'mode': 'overview', 'context': 'synthetic-topic'})['items']
    assert call('corpus_search', {'mode': 'research', 'query': 'researcher authored', 'context': 'synthetic-topic'})['items']
    proposal = call('corpus_reference', {'action': 'propose', 'revision_id': draft['revision_id'], 'operation_id': 'propose'})
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    call('corpus_reference', {'action': 'apply', 'proposal_id': proposal['proposal_id'], 'approval_ref': approval['approval_ref'], 'operation_id': 'apply'})
    rejected = call('corpus_reference', {'action': 'propose', 'revision_id': draft['revision_id'], 'operation_id': 'propose-reject'})
    call('corpus_reference', {'action': 'reject', 'proposal_id': rejected['proposal_id'], 'operation_id': 'reject'})
    batch = call('corpus_reference', {'action': 'propose_batch', 'items': [
        {'action': 'save_source', 'input': {**source, 'content': 'SYNTHETIC batch new source'}},
        {'action': 'save_draft', 'draft': {key: value for key, value in draft_payload.items() if key != 'action'}},
        {'action': 'publish_reference', 'revision_id': draft['revision_id']},
    ], 'operation_id': 'batch'})
    approval = corpus.operator_approve(batch['proposal_id'], batch['hash'], batch['binding'])
    assert call('corpus_reference', {'action': 'apply', 'proposal_id': batch['proposal_id'], 'approval_ref': approval['approval_ref'], 'operation_id': 'apply-batch'})['status'] == 'applied'
    jobs = call('corpus_jobs', {'action': 'list', 'state': 'queued'})['items']
    assert jobs
    call('corpus_jobs', {'action': 'status', 'job_id': jobs[0]['id']})
    call('corpus_jobs', {'action': 'cancel', 'job_id': jobs[0]['id'], 'operation_id': 'cancel'})
    call('corpus_jobs', {'action': 'retry', 'job_id': jobs[0]['id'], 'operation_id': 'retry'})
    other = call('corpus_ingest', {'action': 'submit', 'inputs': [{**source, 'content': 'SYNTHETIC merge candidate'}], 'operation_id': 'other'})['items'][0]
    merge = call('corpus_manage', {'action': 'propose', 'maintenance': 'merge', 'target': other['work_id'], 'expected_revision': 1,
                                   'merge_into': item['work_id'], 'merge_expected_revision': 5, 'reason': 'synthetic', 'operation_id': 'merge'})
    approval = corpus.operator_approve(merge['proposal_id'], merge['hash'], merge['binding'])
    call('corpus_manage', {'action': 'apply', 'proposal_id': merge['proposal_id'], 'approval_ref': approval['approval_ref'], 'operation_id': 'merge-apply'})
    from corpus_core.contracts import CONTRACTS
    assert visited == {(tool, action) for tool, actions in CONTRACTS.items() for action in actions}
