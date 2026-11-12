import json

import pytest

from corpus_core.backfill import backfill
from corpus_core.embeddings import JSONAPITransport
from corpus_core.errors import CorpusError
from test_embed_backfill import library


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv('CORPUS_OFFLINE', '0')
    monkeypatch.setattr('corpus_core.network.validate_url',
                        lambda endpoint: (endpoint, 'synthetic.example', '192.0.2.1'))

    def install(corpus, statuses):
        attempts = []
        responses = iter(statuses)

        def request(*args, **kwargs):
            batch = corpus.db.execute("SELECT id FROM embedding_batches WHERE state='started'").fetchall()
            assert len(batch) == 1
            attempts.append(batch[0]['id'])
            assert not corpus.db.in_transaction
            payload = json.loads(kwargs['body'])
            status = next(responses)
            body = {'data': [{'index': index, 'embedding': [0.25, 0.5, 0.75]}
                             for index in range(len(payload['input']))]}
            return status, {}, json.dumps(body).encode()

        monkeypatch.setattr('corpus_core.network.request', request)
        corpus.embedding.transport = JSONAPITransport('https://synthetic.example/embeddings')
        return attempts

    return install


@pytest.mark.parametrize('status,code,retryable', [
    (429, 'PROVIDER_RATE_LIMITED', True),
    (500, 'PROVIDER_RETRYABLE', True), (502, 'PROVIDER_RETRYABLE', True),
    (503, 'PROVIDER_RETRYABLE', True), (504, 'PROVIDER_RETRYABLE', True),
    (401, 'PROVIDER_FAILED', False), (403, 'PROVIDER_FAILED', False),
])
def test_http_error_semantics(library, provider, status, code, retryable):
    corpus, calls = library
    provider(corpus, [status])
    corpus.db.execute('INSERT INTO embedding_batches VALUES (?,?,?,?,?,?,?)',
                      ('synthetic', 'synthetic', corpus.embedding.profile.id, 'started', 5, None, None))
    with pytest.raises(CorpusError) as caught:
        corpus.embedding.transport({'input': ['alpha']})
    assert caught.value.code == code
    assert caught.value.retryable is retryable


def test_first_429_then_success_same_batch_and_no_profile_lock(library, provider, monkeypatch):
    corpus, calls = library
    attempts = provider(corpus, [429, 200, 200])
    delays = []

    def sleep(delay):
        delays.append(delay)
        assert corpus.db.execute('SELECT state FROM embedding_batches').fetchone()[0] == 'not_sent'
        assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 0
        assert not corpus.db.in_transaction

    monkeypatch.setattr('corpus_core.backfill.time.sleep', sleep)
    result = backfill(corpus, apply=True, limit=1)
    assert result['ok'] and result['embedded'] == 1 and result['remaining'] == 4
    assert result['calls'] == 2 and result['chars'] == 10
    assert result['ambiguous_batches'] == [] and result['stopped_reason'] == 'limit_reached'
    assert attempts[0] == attempts[1] and delays == [2]
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 1
    assert corpus.db.execute('SELECT count(*) FROM embedding_requests').fetchone()[0] == 1
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 1
    assert backfill(corpus, apply=True)['embedded'] == 4
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 5


def test_exhausted_429_can_resume_without_reconciliation(library, provider, monkeypatch):
    corpus, calls = library
    attempts = provider(corpus, [429] * 4)
    delays = []
    monkeypatch.setattr('corpus_core.backfill.time.sleep', delays.append)
    result = backfill(corpus, apply=True, limit=1)
    assert result['ok'] and result['stopped_reason'] == 'rate_limited'
    assert result['embedded'] == 0 and result['remaining'] == result['pending_chunks'] == 5
    assert result['ambiguous_batches'] == [] and 'recovery' not in result
    assert result['calls'] == 4 and delays == [2, 8, 20]
    assert len(set(attempts)) == 1
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 0
    batch = corpus.db.execute('SELECT id,state,error FROM embedding_batches').fetchall()
    assert len(batch) == 1 and batch[0]['state'] == 'not_sent'
    assert batch[0]['error'] == 'PROVIDER_RATE_LIMITED'
    resumed_attempts = provider(corpus, [200])
    resumed = backfill(corpus, apply=True, limit=1)
    assert resumed['ok'] and resumed['embedded'] == 1 and resumed['remaining'] == 4
    assert resumed_attempts == [batch[0]['id']]
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 1
    assert corpus.db.execute('SELECT count(*) FROM embedding_requests').fetchone()[0] == 1
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 1
    assert backfill(corpus, apply=True, max_calls=0)['calls'] == 0


@pytest.mark.parametrize('statuses', [[500], [502], [503], [504], [401], [403], [429, 500]])
def test_uncertain_errors_never_retry_and_lock_profile(library, provider, monkeypatch, statuses):
    corpus, calls = library
    attempts = provider(corpus, statuses)
    monkeypatch.setattr('corpus_core.backfill.time.sleep', lambda delay: None)
    result = backfill(corpus, apply=True, limit=1)
    assert result['stopped_reason'] == 'ambiguous' and len(result['ambiguous_batches']) == 1
    assert result['embedded'] == 0 and result['remaining'] == 5
    assert corpus.db.execute('SELECT state FROM embedding_batches').fetchone()[0] == 'ambiguous'
    blocked = backfill(corpus, apply=True, batch_size=1)
    assert blocked['stopped_reason'] == 'ambiguous' and blocked['calls'] == 0
    assert len(attempts) == len(statuses)


def test_retry_success_continues_next_batch(library, provider, monkeypatch):
    corpus, calls = library
    attempts = provider(corpus, [429, 200, 200])
    delays = []
    monkeypatch.setattr('corpus_core.backfill.time.sleep', delays.append)
    result = backfill(corpus, apply=True, batch_size=1, limit=2,
                      rate_limit_retries=1, rate_limit_delays=(0.5, 1, 2))
    assert result['embedded'] == 2 and result['remaining'] == 3 and result['calls'] == 3
    assert delays == [0.5] and attempts[0] == attempts[1] != attempts[2]
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 2
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 2


@pytest.mark.parametrize('options', [
    {'rate_limit_retries': 0}, {'max_calls': 1}, {'max_chars': 5},
])
def test_retry_never_exceeds_budgets(library, provider, monkeypatch, options):
    corpus, calls = library
    attempts = provider(corpus, [429])
    delays = []
    monkeypatch.setattr('corpus_core.backfill.time.sleep', delays.append)
    result = backfill(corpus, apply=True, limit=1, **options)
    assert result['ok'] and result['stopped_reason'] == 'rate_limited'
    assert result['calls'] == 1 and result['chars'] == 5 and result['remaining'] == 5
    assert len(attempts) == 1 and delays == [] and result['ambiguous_batches'] == []


@pytest.mark.parametrize('options', [
    {'rate_limit_retries': -1}, {'rate_limit_retries': 4}, {'rate_limit_retries': True},
    {'rate_limit_retries': 1.5}, {'rate_limit_retries': '3'},
    {'rate_limit_delays': ()}, {'rate_limit_delays': (1, 2)},
    {'rate_limit_delays': (1, 2, 3, 4)}, {'rate_limit_delays': None},
    {'rate_limit_delays': (-1, 8, 20)}, {'rate_limit_delays': (2, 8, 61)},
    {'rate_limit_delays': (float('nan'), 8, 20)},
    {'rate_limit_delays': (2, float('inf'), 20)},
    {'rate_limit_delays': (True, 8, 20)}, {'rate_limit_delays': ('2', 8, 20)},
])
def test_retry_options_rejected_before_any_writes(library, options):
    corpus, calls = library
    with pytest.raises(CorpusError) as caught:
        backfill(corpus, apply=True, **options)
    assert caught.value.code == 'INVALID_BUDGET'
    assert calls == []
    assert corpus.db.execute('SELECT count(*) FROM embedding_profiles').fetchone()[0] == 0
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 0


@pytest.mark.parametrize('peer_status,reason', [(200, 'concurrent_progress_rerun'), (500, 'ambiguous')])
def test_backoff_rechecks_peer_progress_before_sending(library, provider, monkeypatch, peer_status, reason):
    corpus, calls = library
    attempts = provider(corpus, [429, peer_status])
    peer_results = []

    def peer_run(delay):
        peer_results.append(backfill(corpus, apply=True, limit=1))

    monkeypatch.setattr('corpus_core.backfill.time.sleep', peer_run)
    result = backfill(corpus, apply=True, limit=1)
    assert len(peer_results) == 1 and len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert result['stopped_reason'] == reason and result['calls'] == 1
    assert corpus.db.execute('SELECT count(*) FROM embedding_batches').fetchone()[0] == 1
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == (peer_status == 200)


def test_cli_retry_overrides(library, provider, monkeypatch, root, capsys):
    from corpus_core.cli import main
    from corpus_core.service import Corpus

    corpus, calls = library
    attempts = provider(corpus, [429, 200])
    delays = []
    monkeypatch.setattr('corpus_core.backfill.time.sleep', delays.append)
    monkeypatch.setattr(Corpus, 'from_settings', lambda *args, **kwargs: corpus)
    monkeypatch.setattr(corpus, 'close', lambda: None)
    assert main(['--settings', str(root / 'settings.json'), 'embed-backfill', '--apply',
                 '--limit', '1', '--rate-limit-retries', '1', '--rate-limit-delays', '0', '1', '2']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['embedded'] == 1 and result['calls'] == 2
    assert delays == [0] and attempts[0] == attempts[1]
