import json
from types import SimpleNamespace

import pytest

from corpus_core.backfill import backfill, estimated_tokens
from corpus_core.embeddings import Profile
from corpus_core.errors import CorpusError
from corpus_core.service import Corpus
from corpus_core.settings import EmbeddingRuntime


@pytest.fixture
def library(root):
    corpus = Corpus(root, test_mode=True)
    result = corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC seed', 'title': 'SYNTHETIC'}],
                            'operation_id': 'backfill-seed'})
    generation = 'SYNTHETIC-generation'
    corpus.db.execute('INSERT INTO generations VALUES (?,?,?,?,?,?,?,?)',
                      (generation, result['items'][0]['version_id'], 'SYNTHETIC', '', 'SYNTHETIC', 'complete', '[]', 'SYNTHETIC'))
    for index, text in enumerate(['alpha', 'bravo', 'charm', 'delta', 'eagle']):
        corpus.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',
                          (f'chunk-{index}', generation, index, text, '{}'))
    calls = []
    profile = Profile('dashscope', 'text-embedding-v4', 3)

    def transport(payload):
        calls.append(payload)
        assert corpus.db.execute("SELECT count(*) FROM embedding_batches WHERE state='started'").fetchone()[0] == 1
        return {'data': [{'index': index, 'embedding': [0.123456789, 0.5, 0.75]}
                         for index, text in enumerate(payload['input'])]}

    corpus.embedding = SimpleNamespace(profile=profile, policy={'max_calls': 20, 'max_chars': 32000}, transport=transport)
    yield corpus, calls
    corpus.close()


def test_dry_run_counts_and_no_network_or_database_mutation(library):
    corpus, calls = library
    result = backfill(corpus, batch_size=2, max_chars=12)
    assert result['pending_chunks'] == 5 and result['total_chars'] == 25
    assert result['estimated_batches'] == 3 and result['budget_chunks'] == 2
    assert result['budget_completion_ratio'] == 0.4 and result['remaining'] == 5
    assert calls == []
    assert corpus.db.execute('SELECT count(*) FROM embedding_profiles').fetchone()[0] == 0
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 0


def test_register_dimensions_idempotence_and_no_leaks(library, capsys, caplog):
    corpus, calls = library
    result = backfill(corpus, apply=True, batch_size=2)
    print(json.dumps(result))
    assert result['embedded'] == 5 and result['remaining'] == 0 and result['calls'] == 3
    profiles = corpus.db.execute('SELECT * FROM embedding_profiles').fetchall()
    assert len(profiles) == 1 and profiles[0]['id'] == corpus.embedding.profile.id
    assert json.loads(profiles[0]['config']) == corpus.embedding.profile.config
    assert all(len(json.loads(row[0])) == 3 for row in corpus.db.execute('SELECT vector FROM chunk_embeddings'))
    assert backfill(corpus, apply=True)['calls'] == 0
    assert len(calls) == 3
    output = capsys.readouterr().out + caplog.text
    assert '0.123456789' not in output and 'Bearer' not in output


@pytest.mark.parametrize('budget', [{'max_calls': 1}, {'max_chars': 10}])
def test_budget_stop_and_resume(library, budget):
    corpus, calls = library
    result = backfill(corpus, apply=True, batch_size=2, **budget)
    assert result['embedded'] == 2 and result['remaining'] == 3
    assert result['stopped_reason'] == 'budget_exhausted'
    assert result['calls'] <= budget.get('max_calls', 20)
    assert result['chars'] <= budget.get('max_chars', 32000)
    assert backfill(corpus, apply=True)['embedded'] == 3
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 5


def test_limit(library):
    corpus, calls = library
    result = backfill(corpus, apply=True, limit=3, batch_size=2)
    assert result['embedded'] == 3 and result['remaining'] == 2
    assert result['stopped_reason'] == 'limit_reached'
    assert [len(call['input']) for call in calls] == [2, 1]


def test_dns_failure_retry(library):
    corpus, calls = library
    transport = corpus.embedding.transport

    def failed(payload):
        raise CorpusError('PROVIDER_NOT_SENT', 'SYNTHETIC secret should never be logged')

    corpus.embedding.transport = failed
    result = backfill(corpus, apply=True)
    assert result['stopped_reason'] == 'provider_not_sent' and result['remaining'] == 5
    assert corpus.db.execute('SELECT state FROM embedding_batches').fetchone()[0] == 'not_sent'
    corpus.embedding.transport = transport
    assert backfill(corpus, apply=True)['embedded'] == 5
    assert len(calls) == 1


@pytest.mark.parametrize('error', [CorpusError('PROVIDER_RESPONSE', 'SYNTHETIC_SECRET [0.123456789]'), TimeoutError('SYNTHETIC_SECRET')])
def test_ambiguous_never_retries_even_with_changed_budget(library, error, caplog, capsys):
    corpus, calls = library

    def failed(payload):
        calls.append(payload)
        raise error

    corpus.embedding.transport = failed
    result = backfill(corpus, apply=True)
    assert result['stopped_reason'] == 'ambiguous' and result['remaining'] == 5
    assert len(result['ambiguous_batches']) == 1 and 'reconcile-embedding' in result['recovery']
    assert backfill(corpus, apply=True, batch_size=1, max_chars=1234)['calls'] == 0
    assert len(calls) == 1
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 0
    assert 'SYNTHETIC_SECRET' not in json.dumps(result) + caplog.text + capsys.readouterr().out


def test_interrupt_leaves_started_and_blocks_retry(library):
    corpus, calls = library

    def interrupted(payload):
        raise KeyboardInterrupt()

    corpus.embedding.transport = interrupted
    with pytest.raises(KeyboardInterrupt):
        backfill(corpus, apply=True)
    assert corpus.db.execute('SELECT state FROM embedding_batches').fetchone()[0] == 'started'
    assert backfill(corpus, apply=True)['stopped_reason'] == 'ambiguous'


def test_reconciled_batch_published_without_rebilling(library):
    corpus, calls = library

    def failed(payload):
        raise TimeoutError()

    corpus.embedding.transport = failed
    result = backfill(corpus, apply=True, limit=2)
    runtime = object.__new__(EmbeddingRuntime)
    runtime.profile, runtime.corpus = corpus.embedding.profile, corpus
    runtime.reconcile({'batch_id': result['ambiguous_batches'][0], 'profile_id': runtime.profile.id,
                       'vectors': [[1, 2, 3], [3, 2, 1]], 'evidence': 'SYNTHETIC provider recovery'})
    result = backfill(corpus, apply=True, max_calls=0)
    assert result['calls'] == 0 and result['recovered'] == 2 and result['remaining'] == 3


def test_too_long_skipped_with_first_twenty_ids(library):
    corpus, calls = library
    generation = corpus.db.execute('SELECT generation_id FROM chunks LIMIT 1').fetchone()[0]
    corpus.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)', ('chunk-long', generation, 5, '中' * 12001, '{}'))
    result = backfill(corpus, apply=True)
    assert result['skipped_too_long'] == {'count': 1, 'chunk_ids': ['chunk-long']}
    assert result['embedded'] == 5 and result['remaining'] == 1
    assert estimated_tokens('a' * 32000) == 8000
    assert estimated_tokens('中' * 12001) > 8000
    assert all('中' * 12001 not in call['input'] for call in calls)


def test_batch_char_bound_does_not_truncate_or_send_oversized(library):
    corpus, calls = library
    result = backfill(corpus, apply=True, max_chars=4)
    assert result['embedded'] == result['calls'] == 0 and result['remaining'] == 5
    assert result['stopped_reason'] == 'budget_exhausted' and calls == []


def test_dashscope_batch_downgrade_recorded(library):
    corpus, calls = library
    result = backfill(corpus, apply=True, batch_size=50)
    assert result['batch_size'] == 10 and result['batch_size_downgraded']
    assert len(calls) == 1


def test_cli_report_and_output(library, monkeypatch, root, capsys):
    from corpus_core.cli import main
    corpus, calls = library
    monkeypatch.setattr(Corpus, 'from_settings', lambda *args, **kwargs: corpus)
    monkeypatch.setattr(corpus, 'close', lambda: None)
    report = root / 'report.json'
    assert main(['--settings', str(root / 'settings.json'), 'embed-backfill', '--limit', '2', '--apply', '--report', str(report)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(report.read_text())
    assert json.loads(report.read_text())['embedded'] == 2
