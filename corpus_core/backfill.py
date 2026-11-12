import json
import math
import time

from .db import digest, encoded, transaction
from .embeddings import APIEmbedding
from .errors import CorpusError, require


def estimated_tokens(text):
    return math.ceil(sum(3 if ord(character) < 128 else 8 for character in text) / 12)


def backfill(corpus, *, apply=False, limit=0, batch_size=10, max_chars=None,
             max_calls=None, sleep=0, rate_limit_retries=3, rate_limit_delays=(2, 8, 20)):
    runtime = corpus.embedding
    require(runtime is not None, 'INVALID_SETTINGS', 'explicit embedding settings required')
    max_chars = runtime.policy['max_chars'] if max_chars is None else max_chars
    max_calls = runtime.policy['max_calls'] if max_calls is None else max_calls
    for field, value, minimum in (('limit', limit, 0), ('batch_size', batch_size, 1),
                                   ('max_chars', max_chars, 1), ('max_calls', max_calls, 0)):
        require(type(value) is int and value >= minimum, 'INVALID_BUDGET', field + ' out of bounds')
    require(type(sleep) in (int, float) and math.isfinite(sleep) and sleep >= 0,
            'INVALID_BUDGET', 'sleep must be finite and nonnegative')
    require(type(rate_limit_retries) is int and 0 <= rate_limit_retries <= 3,
            'INVALID_BUDGET', 'rate_limit_retries must be an integer from 0 to 3')
    require(type(rate_limit_delays) in (tuple, list) and len(rate_limit_delays) == 3
            and all(type(delay) in (int, float) and math.isfinite(delay) and 0 <= delay <= 60
                    for delay in rate_limit_delays),
            'INVALID_BUDGET', 'rate_limit_delays must contain three finite intervals from 0 to 60 seconds')
    requested_batch_size = batch_size
    if runtime.profile.provider == 'dashscope' and runtime.profile.exact_model == 'text-embedding-v4':
        batch_size = min(batch_size, 10)
    require(batch_size <= 100, 'INVALID_BUDGET', 'batch_size must be at most 100')
    database = corpus.db
    profile = runtime.profile
    policy_id = f'backfill-{batch_size}-{max_chars}'
    summary = {'ok': True, 'profile_id': profile.id, 'embedded': 0, 'remaining': 0,
               'calls': 0, 'chars': 0, 'stopped_reason': 'dry_run' if not apply else 'complete',
               'ambiguous_batches': [], 'skipped_too_long': {'count': 0, 'chunk_ids': []},
               'requested_batch_size': requested_batch_size, 'batch_size': batch_size,
               'batch_size_downgraded': batch_size != requested_batch_size,
               'max_chars': max_chars, 'max_calls': max_calls, 'recovered': 0}

    def unresolved():
        return [row[0] for row in database.execute(
            "SELECT id FROM embedding_batches WHERE profile_id=? AND state IN ('started','ambiguous') ORDER BY id",
            (profile.id,))]

    def publish_recovered():
        rows = database.execute(
            "SELECT b.vectors,r.request_key,r.input_count FROM embedding_batches b JOIN embedding_requests r ON r.batch_id=b.id "
            "WHERE b.profile_id=? AND b.state='succeeded' AND b.policy_id LIKE 'backfill-%' "
            "AND EXISTS (SELECT 1 FROM json_each(r.request_key,'$.chunk_ids') ids JOIN chunks c ON c.id=ids.value "
            "WHERE NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id AND e.profile_id=b.profile_id))",
            (profile.id,)).fetchall()
        for row in rows:
            key = json.loads(row['request_key'])
            require(type(key) is dict and key.get('kind') == 'embed-backfill',
                    'INVALID_RECONCILIATION', 'backfill chunk mapping required')
            vectors = json.loads(row['vectors'])
            require(len(key['chunk_ids']) == len(vectors) == row['input_count'],
                    'INVALID_RECONCILIATION', 'backfill vector count mismatch')
            for chunk_id, vector in zip(key['chunk_ids'], vectors):
                if database.execute('SELECT 1 FROM chunks WHERE id=?', (chunk_id,)).fetchone() is None:
                    continue
                cursor = database.execute('INSERT OR IGNORE INTO chunk_embeddings VALUES (?,?,?)',
                                          (chunk_id, profile.id, encoded(profile.vector(vector))))
                summary['recovered'] += cursor.rowcount

    if apply:
        with transaction(database):
            database.execute('INSERT OR IGNORE INTO embedding_profiles VALUES (?,?)', (profile.id, encoded(profile.config)))
            publish_recovered()
    missing = database.execute(
        'SELECT id,text FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id AND e.profile_id=?) ORDER BY c.id',
        (profile.id,)).fetchall()
    summary['pending_chunks'] = len(missing)
    summary['total_chars'] = sum(len(row['text']) for row in missing)
    eligible = []
    for row in missing:
        if estimated_tokens(row['text']) > 8000:
            skipped = summary['skipped_too_long']
            skipped['count'] += 1
            if len(skipped['chunk_ids']) < 20:
                skipped['chunk_ids'].append(row['id'])
        else:
            eligible.append(row)
    selected = eligible[:limit] if limit else eligible

    def batches(rows):
        batch = []
        chars = 0
        for row in rows:
            size = len(row['text'])
            if batch and (len(batch) >= batch_size or chars + size > max_chars):
                yield batch
                batch, chars = [], 0
            if size > max_chars:
                if batch:
                    yield batch
                    batch, chars = [], 0
                yield [row]
            else:
                batch.append(row)
                chars += size
        if batch:
            yield batch

    planned = list(batches(selected))
    summary['estimated_batches'] = len(planned)
    affordable = 0
    projected_chars = 0
    for batch in planned[:max_calls]:
        size = sum(len(row['text']) for row in batch)
        if projected_chars + size > max_chars:
            break
        projected_chars += size
        affordable += len(batch)
    summary['budget_chunks'] = affordable
    summary['budget_completion_ratio'] = affordable / len(missing) if missing else 1.0
    summary['ambiguous_batches'] = unresolved()
    if apply and summary['ambiguous_batches']:
        summary['stopped_reason'] = 'ambiguous'
    elif apply:
        for batch in planned:
            texts = [row['text'] for row in batch]
            chars = sum(map(len, texts))
            if summary['calls'] >= max_calls or summary['chars'] + chars > max_chars:
                summary['stopped_reason'] = 'budget_exhausted'
                break
            key = {'kind': 'embed-backfill', 'chunk_ids': [row['id'] for row in batch]}
            batch_id = digest({'profile': profile.id, 'key': key, 'texts': texts})
            succeeded = False
            for attempt in range(rate_limit_retries + 1):
                with transaction(database):
                    blocked = unresolved()
                    if blocked:
                        summary['ambiguous_batches'] = blocked
                        summary['stopped_reason'] = 'ambiguous'
                        break
                    already_done = database.execute(
                        'SELECT count(*) FROM chunk_embeddings WHERE profile_id=? AND chunk_id IN (SELECT value FROM json_each(?))',
                        (profile.id, encoded(key['chunk_ids']))).fetchone()[0]
                    if already_done:
                        summary['stopped_reason'] = 'concurrent_progress_rerun'
                        break
                    old = database.execute('SELECT state FROM embedding_batches WHERE id=?', (batch_id,)).fetchone()
                    require(old is None or old['state'] == 'not_sent', 'RECONCILIATION_CONFLICT', 'batch must not be billed twice')
                    if old:
                        database.execute("UPDATE embedding_batches SET state='started',policy_id=?,error=NULL WHERE id=?", (policy_id, batch_id))
                    else:
                        database.execute('INSERT INTO embedding_batches VALUES (?,?,?,?,?,?,?)',
                                         (batch_id, policy_id, profile.id, 'started', chars, None, None))
                        database.execute('INSERT INTO embedding_requests VALUES (?,?,?)', (batch_id, len(texts), encoded(key)))
                summary['calls'] += 1
                summary['chars'] += chars
                try:
                    vectors = APIEmbedding(profile, runtime.transport, max_texts=batch_size, max_chars=max_chars)(texts)
                except Exception as error:
                    rate_limited = isinstance(error, CorpusError) and error.code == 'PROVIDER_RATE_LIMITED'
                    not_sent = rate_limited or isinstance(error, CorpusError) and error.code in (
                        'PROVIDER_NOT_SENT', 'NETWORK_DISABLED', 'MISSING_KEY', 'INVALID_AUTH', 'UNSAFE_URL')
                    with transaction(database):
                        database.execute("UPDATE embedding_batches SET state=?,error=? WHERE id=? AND state='started'",
                                         ('not_sent' if not_sent else 'ambiguous',
                                          'PROVIDER_RATE_LIMITED' if rate_limited else
                                          'PROVIDER_NOT_SENT' if not_sent else 'PROVIDER_RESPONSE', batch_id))
                    summary['stopped_reason'] = 'rate_limited' if rate_limited else 'provider_not_sent' if not_sent else 'ambiguous'
                    summary['ambiguous_batches'] = unresolved()
                    if (rate_limited and attempt < rate_limit_retries and summary['calls'] < max_calls
                            and summary['chars'] + chars <= max_chars):
                        time.sleep(rate_limit_delays[attempt])
                        continue
                    break
                succeeded = True
                summary['stopped_reason'] = 'complete'
                break
            if not succeeded:
                break
            with transaction(database):
                current = database.execute('SELECT state FROM embedding_batches WHERE id=?', (batch_id,)).fetchone()
                require(current['state'] == 'started', 'RECONCILIATION_CONFLICT', 'batch state changed during provider call')
                for row, vector in zip(batch, vectors):
                    database.execute('INSERT OR IGNORE INTO chunk_embeddings VALUES (?,?,?)', (row['id'], profile.id, encoded(vector)))
                database.execute("UPDATE embedding_batches SET state='succeeded',vectors=? WHERE id=?", (encoded(vectors), batch_id))
            summary['embedded'] += len(batch)
            if sleep and summary['embedded'] < len(selected):
                time.sleep(sleep)
        else:
            if len(selected) < len(eligible):
                summary['stopped_reason'] = 'limit_reached'
            elif summary['skipped_too_long']['count']:
                summary['stopped_reason'] = 'skipped_too_long'
    summary['remaining'] = database.execute(
        'SELECT count(*) FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id AND e.profile_id=?)',
        (profile.id,)).fetchone()[0]
    if summary['ambiguous_batches']:
        summary['recovery'] = 'Run local operator reconcile-embedding with recovered vectors and evidence; never retry uncertain billing automatically.'
    return summary
