import json

import pytest

from corpus_core.service import Corpus


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")
from test_retrieval import seed


def test_six_sdk_shaped_tools_and_strict_actions(root, monkeypatch):
    from corpus_core.plugin import register
    from corpus_core.contracts import CONTRACTS
    monkeypatch.setenv('CORPUS_SANDBOX_ROOT', str(root))
    captured = {}

    class SignatureRecorder:
        def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description='', emoji='', override=False):
            captured[name] = (schema, handler)

    register(SignatureRecorder())
    assert len(captured) == 6
    for name, (schema, handler) in captured.items():
        assert schema['name'] == name
        assert schema['parameters']['properties']
        assert 'oneOf' not in schema['parameters']
        assert json.loads(handler({'garbage': True}))['ok'] is False
        for action, specification in CONTRACTS[name].items():
            invalid = {specification['tag']: action, 'unexpected': True}
            assert json.loads(handler(invalid, session_id='test'))['error']['code'] == 'INVALID_ARGUMENT'
    handler = captured['corpus_ingest'][1]
    request = {'action': 'submit', 'operation_id': 'sdk-seed', 'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC tool test'}]}
    first = json.loads(handler(request))
    assert first['ok']
    assert json.loads(handler(request))['result'] == first['result']
    assert not json.loads(handler({**request, 'approved': True}))['ok']


def test_jobs_metadata_external_links_and_export_failure(root):
    from corpus_core.tools import dispatch
    corpus = Corpus(root)
    item = seed(corpus)
    result = dispatch(corpus, 'corpus_manage', {'action': 'metadata', 'target': item['work_id'], 'expected_revision': 1, 'changes': {'title': 'SYNTHETIC correction'}, 'source_ref': 'corpus://version/' + item['version_id'], 'operation_id': 'meta'})
    assert result['revision'] == 2
    dispatch(corpus, 'corpus_manage', {'action': 'external_link', 'target': 'corpus://version/' + item['version_id'], 'uri': 'wiki:research/test.md', 'basis': 'direct citation', 'operation_id': 'wiki'})
    assert corpus.db.execute('SELECT count(*) FROM external_links').fetchone()[0] == 1
    result = dispatch(corpus, 'corpus_jobs', {'action': 'list', 'limit': 1})
    assert result['items']
    from test_research import draft
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_publish(revision['revision_id'], 'p')
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'a')
    (root / 'reference-exports').write_text('SYNTHETIC obstructed export')
    assert corpus.export_one()['state'] == 'failed'
    assert corpus.read('corpus://reference/' + revision['record_id'])['formal']
    (root / 'reference-exports').unlink()
    assert corpus.export_one()['state'] == 'succeeded'
