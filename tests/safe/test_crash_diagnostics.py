import json
import time

import pytest


@pytest.mark.parametrize('script,reason', [
    ("import sys; print('SYNTHETIC early stdout'); print('SYNTHETIC early stderr', file=sys.stderr); sys.exit(7)", 'early_exit'),
    ("print('SYNTHETIC wrong checkpoint', flush=True)", 'missing_readiness'),
    ("import time; time.sleep(30)", 'timeout'),
    ("import sys,time; sys.stdout.write('SYNTHETIC_READY'); sys.stdout.flush(); time.sleep(30)", 'timeout'),
])
def test_checkpoint_failure_retains_bounded_diagnostics(root, script, reason):
    from test_crash_recovery import wait_checkpoint
    root.mkdir(parents=True)
    started = time.monotonic()
    with pytest.raises(AssertionError, match=reason):
        wait_checkpoint(script, root, timeout=0.3)
    assert time.monotonic() - started < 5
    evidence = json.loads((root / 'child-execution.json').read_text())
    assert evidence['reason'] == reason
    assert evidence['returncode'] is not None
    assert evidence['command'] and evidence['cwd'] and evidence['executable']
    if reason == 'early_exit':
        assert evidence['returncode'] == 7
        assert 'early stderr' in evidence['stderr'] and 'early stdout' in evidence['stdout']


def test_expired_short_lease_reproduces_pre_checkpoint_exit(root):
    from corpus_core.service import Corpus
    from test_crash_recovery import wait_checkpoint
    from test_retrieval import seed
    corpus = Corpus(root)
    item = seed(corpus)
    corpus.db.execute("UPDATE jobs SET state='queued',payload=? WHERE id=?",
                      (json.dumps({'version_id': item['version_id'], 'pipeline': 'SYNTHETIC-delayed-parse'}), item['job_id']))
    script = '''
import json,sys,time
import corpus_core.service as service
original_parse = service.parse
def delayed_parse(*args):
    time.sleep(0.05)
    return original_parse(*args)
service.parse = delayed_parse
corpus = service.Corpus(sys.argv[1])
print(json.dumps(corpus.run_one(lease_seconds=0.01)), flush=True)
'''
    with pytest.raises(AssertionError, match='missing_readiness'):
        wait_checkpoint(script, root)
    evidence = json.loads((root / 'child-execution.json').read_text())
    assert 'LEASE_LOST' in evidence['stdout'] and evidence['returncode'] == 0
    assert corpus.job(item['job_id'])['state'] == 'failed'
    assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 1
    corpus.jobs_control('retry', item['job_id'], 'synthetic-retry')
    assert corpus.run_one()['state'] == 'succeeded'
    corpus.close()
