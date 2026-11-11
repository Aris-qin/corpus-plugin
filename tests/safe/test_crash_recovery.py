import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus
from test_retrieval import seed


def wait_checkpoint(script, root, timeout=10, on_ready=None):
    source = Path(__file__).resolve().parents[2]
    bootstrap = 'import sys; sys.path.insert(0, ' + repr(str(source)) + ');\n'
    command = [sys.executable, '-I', '-B', '-u', '-c', bootstrap + script, str(root)]
    environment = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'CORPUS_SETTINGS_FILE')}
    evidence = {'command': command, 'cwd': str(source), 'executable': sys.executable,
                'python': sys.version, 'timeout_seconds': timeout,
                'environment': {key: environment.get(key) for key in ('HOME', 'TMPDIR', 'CORPUS_OFFLINE', 'CORPUS_TEST_ROOT')},
                'reason': 'timeout'}
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    started = time.monotonic()
    child = subprocess.Popen(command, cwd=source, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    failure = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(child.stderr, selectors.EVENT_READ, 'stderr')
            while time.monotonic() - started < timeout:
                for key, events in selector.select(max(0, timeout - (time.monotonic() - started))):
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(data)
                if any(len(value) > 65536 for value in output.values()):
                    evidence['reason'] = 'output_limit'
                    break
                if b'SYNTHETIC_READY_TO_KILL\n' in output['stdout'].splitlines(keepends=True):
                    if on_ready is not None:
                        on_ready()
                    evidence['reason'] = 'checkpoint_killed'
                    break
                if not selector.get_map():
                    child.wait(timeout=1)
                    evidence['reason'] = 'early_exit' if child.returncode else 'missing_readiness'
                    break
    except BaseException as error:
        evidence['reason'] = 'checkpoint_assertion_failed'
        failure = error
    finally:
        evidence['returncode_before_cleanup'] = child.poll()
        if child.poll() is None:
            child.kill()
        remaining_stdout, remaining_stderr = child.communicate(timeout=2)
        output['stdout'].extend(remaining_stdout)
        output['stderr'].extend(remaining_stderr)
        evidence.update({name: bytes(value[:65536]).decode('utf-8', errors='replace') for name, value in output.items()})
        evidence['returncode'] = child.returncode
        evidence['elapsed_seconds'] = time.monotonic() - started
        (root / 'child-execution.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    if failure is not None:
        raise AssertionError(json.dumps(evidence)) from failure
    assert evidence['reason'] == 'checkpoint_killed', json.dumps(evidence)
    assert evidence['returncode_before_cleanup'] is None and child.returncode < 0, json.dumps(evidence)
    return evidence


def test_real_process_kill_before_generation_activation_recovers(root):
    corpus = Corpus(root)
    item = seed(corpus)
    old = corpus.search('evidence')['hits'][0]['ref']
    old_generation = corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0]
    corpus.db.execute("UPDATE jobs SET state='queued',payload=? WHERE id=?", (json.dumps({'version_id': item['version_id'], 'pipeline': 'SYNTHETIC-crash-rebuild'}), item['job_id']))
    script = '''
import json, pathlib, sys, time
import corpus_core.service as service
assert pathlib.Path(service.__file__).resolve().is_relative_to(pathlib.Path.cwd())
print(json.dumps({'service_file': service.__file__, 'executable': sys.executable}), flush=True)
original = service.require
def intercept(condition, code, message):
    if message == 'activation fault injection':
        assert corpus.db.in_transaction
        assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 2
        assert corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0] == sys.argv[2]
        print('SYNTHETIC_READY_TO_KILL', flush=True)
        time.sleep(30)
    return original(condition, code, message)
service.require = intercept
corpus = service.Corpus(sys.argv[1])
result = corpus.run_one(lease_seconds=2)
print(json.dumps({'unexpected_completion': result}), flush=True)
'''
    script = 'import sys; sys.argv.append(' + repr(old_generation) + ');\n' + script
    def prior_generation_visible():
        assert corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0] == old_generation
        assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 1
        assert corpus.read(old)['chunk']
    wait_checkpoint(script, root, on_ready=prior_generation_visible)
    assert corpus.db.execute('SELECT generation_id FROM active_generations').fetchone()[0] == old_generation
    assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 1
    assert corpus.read(old)['chunk']
    lease_until = corpus.job(item['job_id'])['lease_until']
    assert lease_until - time.time() < 3
    time.sleep(max(0, lease_until - time.time()) + 0.02)
    assert corpus.run_one()['state'] == 'succeeded'
    assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 2
    assert corpus.read(old)['chunk']
    corpus.close()


def test_db_busy_is_retryable_without_partial_receipt(root):
    corpus = Corpus(root)
    item = seed(corpus)
    rival = Corpus(root)
    corpus.db.execute('PRAGMA busy_timeout=0')
    rival.db.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(CorpusError) as caught:
            corpus.link(item['work_id'], 'synthetic-topic', 'busy-link')
        assert caught.value.code == 'DB_BUSY' and caught.value.retryable
    finally:
        rival.db.execute('ROLLBACK')
        rival.close()
    assert corpus.link(item['work_id'], 'synthetic-topic', 'busy-link')['context'] == 'synthetic-topic'
