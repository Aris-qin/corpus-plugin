import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus
from test_bounded_integrations import settings


@pytest.mark.parametrize('package_mode', [True, False])
def test_directory_entrypoint_without_candidate_import_path(root, package_mode):
    plugin = root / 'hermes-home' / 'plugins' / 'corpus-candidate'
    plugin.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    shutil.copy(source / '__init__.py', plugin)
    shutil.copytree(source / 'corpus_core', plugin / 'corpus_core')
    script = '''
import importlib.machinery, importlib.util, pathlib, sys
if sys.argv[2] == 'True':
    spec = importlib.util.spec_from_file_location('isolated_corpus', sys.argv[1], submodule_search_locations=[str(pathlib.Path(sys.argv[1]).parent)])
else:
    loader = importlib.machinery.SourceFileLoader('isolated_corpus', sys.argv[1])
    spec = importlib.util.spec_from_loader(loader.name, loader, is_package=False)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.__package__ == module.__spec__.parent
assert len(sys.modules['isolated_corpus.corpus_core.contracts'].CONTRACTS) == 6
assert 'corpus_core' not in sys.modules
print(module.register.__module__)
'''
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(plugin / '__init__.py'), str(package_mode)],
                            cwd=root, env=environment, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def transport(payload):
    return {'data': [{'index': index, 'embedding': [1.0, 0.0]} for index, text in enumerate(payload['input'])]}


def submit(corpus):
    return corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'evidence first\nsecond'}],
                          'operation_id': 'submit'})['items'][0]


def test_partial_budget_resume_without_reparse_or_rebill(root, monkeypatch):
    path = settings(root, max_calls=1)
    calls = []
    def counted(payload):
        calls.append(payload)
        return transport(payload)
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=counted)
    item = submit(corpus)
    job = corpus.run_one()
    assert job['state'] == 'partial'
    assert json.loads(job['checkpoint'])['embedded_chunks'] == 1
    generation = json.loads(job['checkpoint'])['generation_id']
    assert 'index_incomplete' in corpus.search('evidence')['warnings']
    corpus.close()
    payload = json.loads(path.read_text())
    payload['embedding']['policy']['max_calls'] = 4
    path.write_text(json.dumps(payload))
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=counted)
    corpus.jobs_control('retry', item['job_id'], 'resume')
    monkeypatch.setattr('corpus_core.service.parse', lambda *args: pytest.fail('completed parsing repeated'))
    assert corpus.run_one()['state'] == 'succeeded'
    assert corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0] == generation
    assert len(calls) == 2
    assert corpus.jobs_control('retry', item['job_id'], 'resume')['state'] == 'queued'


@pytest.mark.parametrize('failure', ['timeout', 'zero'])
def test_ambiguous_partial_requires_review(root, failure):
    def broken(payload):
        if failure == 'timeout':
            raise TimeoutError('SYNTHETIC billed result unknown')
        return {'data': [{'index': 0, 'embedding': [0.0, 0.0]}]}
    corpus = Corpus.from_settings(settings(root), test_mode=True, embedding_transport=broken)
    item = submit(corpus)
    assert corpus.run_one()['state'] == 'partial'
    with pytest.raises(CorpusError, match='AMBIGUOUS_REVIEW_REQUIRED'):
        corpus.jobs_control('retry', item['job_id'], 'unsafe')
    assert corpus.job(item['job_id'])['state'] == 'partial'
    assert corpus.db.execute('SELECT count(*) FROM receipts WHERE operation_id="unsafe"').fetchone()[0] == 0


def test_default_operator_and_old_approval_are_disabled(root):
    corpus = Corpus(root)
    item = submit(corpus)
    corpus.run_one()
    corpus.lifecycle(item['work_id'], 'recycled', 1, 'recycle')
    proposal = corpus.propose_maintenance({'action': 'propose', 'maintenance': 'purge', 'target': item['work_id'],
                                          'expected_revision': 2, 'operation_id': 'proposal'})
    with pytest.raises(CorpusError, match='SENSITIVE_ACTION_DISABLED'):
        corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    corpus.db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?)', ('old-simulated', proposal['proposal_id'], proposal['hash'], proposal['binding'], 0, 'old'))
    corpus.db.execute("UPDATE proposals SET state='approved' WHERE id=?", (proposal['proposal_id'],))
    before = corpus.db.total_changes
    with pytest.raises(CorpusError, match='SENSITIVE_ACTION_DISABLED'):
        corpus.apply_proposal(proposal['proposal_id'], 'old-simulated', 'apply')
    assert corpus.db.total_changes == before
    assert corpus.db.execute('SELECT count(*) FROM version_assets').fetchone()[0] == 1


def test_strict_disabled_setting_and_no_simulation_via_settings(root):
    path = settings(root)
    payload = json.loads(path.read_text())
    payload['safety_mode'] = 'disabled'
    path.write_text(json.dumps(payload))
    corpus = Corpus.from_settings(path)
    assert corpus.safety_mode == 'disabled'
    corpus.close()
    payload['safety_mode'] = 'test_simulation'
    path.write_text(json.dumps(payload))
    with pytest.raises(CorpusError, match='INVALID_SETTINGS'):
        Corpus.from_settings(path, test_mode=True)
    with pytest.raises(CorpusError):
        Corpus(root / 'cannot-enable', test_mode=True, safety_mode='test_simulation')
    corpus = Corpus(root / 'transport-test-mode', test_mode=True)
    with pytest.raises(CorpusError) as blocked:
        corpus.operator_approve('fake', 'fake', 'fake')
    assert blocked.value.code == 'SENSITIVE_ACTION_DISABLED'


def test_legacy_review_inventory_is_actionable(root):
    corpus = Corpus(root)
    corpus.db.execute("INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES ('synthetic','orphan','orphan_vector_preserved')")
    report = corpus.legacy_inventory()
    assert report['issues'][0]['legacy_id'] == 'orphan'
    assert report['vector_policy'] == 'unknown_remains_quarantined_no_reembedding'
    with pytest.raises(CorpusError, match='PROVENANCE_REQUIRED'):
        corpus.claim_legacy_profile({'source_hash': 'synthetic', 'profile': {'provider': 'api', 'exact_model': 'guessed', 'dim': 2}})


def test_reconcile_recovered_vectors_never_reissues_provider_request(root):
    def broken(payload):
        raise TimeoutError('SYNTHETIC billed unknown')
    path = settings(root)
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=broken)
    item = submit(corpus)
    assert corpus.run_one()['state'] == 'partial'
    batch = corpus.embedding.recovery_inventory()['batches'][0]
    assert json.loads(batch['request_key'])['version'] == item['version_id']
    manifest = {'batch_id': batch['id'], 'profile_id': corpus.embedding.profile.id, 'vectors': [[1.0, 0.0]], 'evidence': 'SYNTHETIC provider recovered response'}
    with pytest.raises(CorpusError):
        corpus.embedding.reconcile({**manifest, 'vectors': []})
    assert not corpus.embedding.reconcile(manifest)['replayed']
    assert corpus.embedding.reconcile(manifest)['replayed']
    with pytest.raises(CorpusError):
        corpus.embedding.reconcile({**manifest, 'vectors': [[0.0, 1.0]]})
    corpus.embedding.fixture_transport = transport
    corpus.jobs_control('retry', item['job_id'], 'resume-reviewed')
    assert corpus.run_one()['state'] == 'succeeded'


def test_zero_index_budget_and_previous_active_generation_survive(root):
    path = settings(root, max_calls=1)
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
    item = submit(corpus)
    assert corpus.run_one()['state'] == 'partial'
    first = corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0]
    payload = json.loads(corpus.job(item['job_id'])['payload'])
    payload['pipeline'] = 'SYNTHETIC-new-pipeline'
    corpus.db.execute("UPDATE jobs SET state='queued',payload=? WHERE id=?", (json.dumps(payload), item['job_id']))
    job = corpus.run_one()
    assert job['state'] == 'partial'
    assert json.loads(job['checkpoint'])['embedded_chunks'] == 0
    assert corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0] == first
    assert corpus.search('evidence')['hits']


def test_historical_incomplete_success_is_reclassified_without_erasing_steps(root):
    path = settings(root, max_calls=1)
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
    item = submit(corpus)
    corpus.run_one()
    corpus.db.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (item['job_id'],))
    steps = [tuple(row) for row in corpus.db.execute('SELECT * FROM job_steps')]
    corpus.close()
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
    assert corpus.job(item['job_id'])['state'] == 'partial'
    assert [tuple(row) for row in corpus.db.execute('SELECT * FROM job_steps')] == steps
    assert corpus.db.execute("SELECT count(*) FROM audit WHERE event='incomplete_index_state_repaired'").fetchone()[0] == 1


@pytest.mark.parametrize('entry', ['tool_reference', 'tool_manage', 'cli_operator', 'direct_purge', 'direct_recovery', 'direct_batch', 'direct_publish'])
def test_sensitive_entrypoints_default_deny_without_writes(root, monkeypatch, entry):
    corpus = Corpus(root)
    before = corpus.db.total_changes
    if entry.startswith('tool_'):
        from corpus_core.plugin import handler_for
        monkeypatch.setenv('CORPUS_SANDBOX_ROOT', str(root))
        tool = 'corpus_reference' if entry == 'tool_reference' else 'corpus_manage'
        result = json.loads(handler_for(tool)({'action': 'apply', 'proposal_id': 'fake', 'approval_ref': 'model-confirmed', 'operation_id': 'unsafe'}))
        assert result['error']['code'] == 'SENSITIVE_ACTION_DISABLED'
    elif entry == 'cli_operator':
        result = subprocess.run([sys.executable, '-m', 'corpus_core.cli', '--root', str(root), 'operator-approve', '--proposal-id', 'fake', '--hash', 'fake', '--binding', 'fake'], text=True, capture_output=True)
        assert result.returncode == 1 and json.loads(result.stdout)['error']['code'] == 'SENSITIVE_ACTION_DISABLED'
    else:
        with pytest.raises(CorpusError) as blocked:
            if entry == 'direct_purge':
                corpus.apply_purge('fake', 'fake', 'unsafe', {})
            elif entry == 'direct_recovery':
                corpus.recover_purge('unsafe')
            elif entry == 'direct_batch':
                corpus._apply_batch('fake', 'fake', 'unsafe', {}, {})
            else:
                corpus._publish_reference({})
        assert blocked.value.code == 'SENSITIVE_ACTION_DISABLED'
    assert corpus.db.total_changes == before
    assert corpus.db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0


def test_partial_retry_concurrent_receipts_and_runner_lease(root):
    from concurrent.futures import ThreadPoolExecutor
    path = settings(root, max_calls=1)
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
    item = submit(corpus)
    assert corpus.run_one()['state'] == 'partial'
    payload = json.loads(path.read_text())
    payload['embedding']['policy']['max_calls'] = 4
    path.write_text(json.dumps(payload))
    def retry(unused):
        client = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
        try:
            return client.jobs_control('retry', item['job_id'], 'concurrent-resume')
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(retry, range(2)))
    assert results[0] == results[1] and results[0]['state'] == 'queued'
    def run(unused):
        client = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
        try:
            return client.run_one()
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert sum(result is None for result in results) == 1
    assert corpus.job(item['job_id'])['attempt'] == 2
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 2


def test_partial_retry_rejects_endpoint_rebinding(root):
    path = settings(root, max_calls=1)
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
    item = submit(corpus)
    corpus.run_one()
    payload = json.loads(path.read_text())
    payload['embedding']['endpoint'] = 'https://different.example.test/embeddings'
    path.write_text(json.dumps(payload))
    other = Corpus.from_settings(path, test_mode=True, embedding_transport=transport)
    with pytest.raises(CorpusError) as blocked:
        other.jobs_control('retry', item['job_id'], 'unsafe-rebinding')
    assert blocked.value.code == 'CONFIGURATION_CHANGED'
    assert other.job(item['job_id'])['state'] == 'partial'


def test_old_formal_export_and_receipt_cannot_bypass_default(root, monkeypatch):
    from test_research import draft
    corpus = Corpus(root)
    item = submit(corpus)
    corpus.run_one()
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    proposal = corpus.propose_publish(revision['revision_id'], 'proposal')
    with monkeypatch.context() as simulation:
        simulation.setattr(Corpus, 'require_sensitive_actions', lambda self: None)
        approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
        corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'historical-simulated')
    before = corpus.db.total_changes
    with pytest.raises(CorpusError) as blocked:
        corpus.export_one()
    assert blocked.value.code == 'SENSITIVE_ACTION_DISABLED'
    with pytest.raises(CorpusError):
        corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'historical-simulated')
    assert corpus.db.total_changes == before
    assert not (root / 'reference-exports').exists()


@pytest.mark.parametrize('preflight', ['offline', 'dns'])
def test_definitely_unsent_provider_failures_are_safely_resumable(root, monkeypatch, preflight):
    if preflight == 'dns':
        import socket
        def unresolved(*args):
            raise socket.gaierror('SYNTHETIC pre-send DNS failure')
        monkeypatch.delenv('CORPUS_OFFLINE', raising=False)
        monkeypatch.setenv('SYNTHETIC_UNUSED_KEY', 'SYNTHETIC-not-a-real-key')
        monkeypatch.setattr('corpus_core.network.validate_url', unresolved)
    corpus = Corpus.from_settings(settings(root), test_mode=True)
    item = submit(corpus)
    job = corpus.run_one()
    assert job['state'] == 'partial'
    assert json.loads(job['checkpoint'])['vector_status'] == 'provider_not_sent'
    assert corpus.embedding.recovery_inventory()['batches'][0]['state'] == 'not_sent'
    corpus.embedding.fixture_transport = transport
    corpus.jobs_control('retry', item['job_id'], 'safe-resume')
    assert corpus.run_one()['state'] == 'succeeded'
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 2


def test_late_provider_result_cannot_overwrite_operator_reconciliation(root):
    def late(payload):
        batch = corpus.embedding.recovery_inventory()['batches'][0]
        corpus.db.execute("UPDATE jobs SET lease_until=0 WHERE state='running'")
        corpus.embedding.reconcile({'batch_id': batch['id'], 'profile_id': corpus.embedding.profile.id,
                                    'vectors': [[1.0, 0.0]], 'evidence': 'SYNTHETIC recovered response after expired lease'})
        return {'data': [{'index': 0, 'embedding': [0.0, 1.0]}]}
    corpus = Corpus.from_settings(settings(root), test_mode=True, embedding_transport=late)
    corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'one chunk'}], 'operation_id': 'submit'})
    assert corpus.run_one()['state'] == 'failed'
    assert json.loads(corpus.db.execute('SELECT vectors FROM embedding_batches').fetchone()[0]) == [[1.0, 0.0]]
