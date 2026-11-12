import hashlib
import importlib
import json
import math
import os
import struct
from concurrent.futures import ThreadPoolExecutor

import pytest

from corpus_core.db import encoded
from corpus_core.embeddings import Profile
from corpus_core.errors import CorpusError
from test_candidate_budget import library


@pytest.fixture
def vectors():
    module = importlib.import_module('corpus_core.vector_cache')
    module.CACHE.clear()
    yield module
    module.CACHE.clear()


def vector_library(root, count=30, dim=4):
    corpus, _ = library(root, count)
    profile = Profile('mock', 'SYNTHETIC-binary', dim)
    corpus.db.execute('INSERT INTO embedding_profiles VALUES (?,?)', (profile.id, encoded(profile.config)))
    corpus.db.executemany('INSERT INTO chunk_embeddings VALUES (?,?,?)',
        ((f'budget_{ordinal:06d}', profile.id, encoded([math.sin(ordinal + axis) + 1.1 for axis in range(dim)]))
         for ordinal in range(count)))
    return corpus, profile


def search(corpus, profile):
    return corpus.search('needle', candidate_budget=20, top_k=5,
                         profile=profile, query_vector=[1.0] * profile.dim)


def assert_parity(actual, expected):
    assert [hit['ref'] for hit in actual['hits']] == [hit['ref'] for hit in expected['hits']]
    assert [hit['rrf_score'] for hit in actual['hits']] == [hit['rrf_score'] for hit in expected['hits']]
    assert [hit['vector_score'] for hit in actual['hits']] == pytest.approx(
        [hit['vector_score'] for hit in expected['hits']], abs=1e-7)
    assert actual['truncated'] == expected['truncated']
    assert actual['status'] == expected['status']


def test_build_artifact_format(root, vectors):
    corpus, profile = vector_library(root, 5000, 64)
    result = vectors.build(corpus, profile.id, apply=True)
    data_path, metadata_path = vectors.artifact_paths(root, profile.id, result['fingerprint'])
    metadata = json.loads(metadata_path.read_text())
    assert data_path.stat().st_size == 5000 * 64 * 4
    assert metadata['schema'] == 1 and metadata['dtype'] == 'float32-le'
    assert metadata['profile_id'] == profile.id and metadata['dim'] == 64 and metadata['count'] == 5000
    assert metadata['fingerprint'] == result['fingerprint'] and metadata['created_at']
    expected_index = hashlib.sha256()
    for ordinal in range(5000):
        expected_index.update((encoded([f'budget_{ordinal:06d}', ordinal]) + '\n').encode())
    assert metadata['index_sha256'] == expected_index.hexdigest()
    assert vectors.verify(corpus, profile.id)['ok'] is True
    corpus.close()


def test_build_idempotent_and_atomic(root, vectors, monkeypatch):
    corpus, profile = vector_library(root)
    dry = vectors.build(corpus, profile.id)
    assert dry['dry_run'] is True and not (root / 'runtime' / 'vectors').exists()
    first = vectors.build(corpus, profile.id, apply=True)
    paths = vectors.artifact_paths(root, profile.id, first['fingerprint'])
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert vectors.build(corpus, profile.id, apply=True)['fingerprint'] == first['fingerprint']
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    paths[0].unlink()

    def failed_replace(*args):
        raise OSError('SYNTHETIC interrupted publish')

    monkeypatch.setattr(vectors.os, 'replace', failed_replace)
    with pytest.raises(OSError):
        vectors.build(corpus, profile.id, apply=True)
    assert not paths[0].exists()
    assert not list(paths[0].parent.glob('*.tmp'))
    corpus.close()


def test_parity_with_json_path(root, vectors, monkeypatch):
    corpus, profile = vector_library(root)
    expected = search(corpus, profile)
    vectors.build(corpus, profile.id, apply=True)
    actual = search(corpus, profile)
    assert 'vector_binary_scan' in actual['warnings']
    assert_parity(actual, expected)
    corpus.link(corpus.db.execute('SELECT id FROM works').fetchone()[0], 'SYNTHETIC-context', 'link')
    for arguments in ({'filters': {'work_ids': ['absent']}}, {'scope': 'linked', 'context': 'absent'},
                      {'scope': 'linked', 'context': 'SYNTHETIC-context'}, {'filters': {'version_kinds': ['formal']}}):
        binary = corpus.search('needle', profile=profile, query_vector=[1.0] * profile.dim, **arguments)
        with monkeypatch.context() as patch:
            patch.setenv('CORPUS_VECTOR_CACHE_BYTES', '0')
            baseline = corpus.search('needle', profile=profile, query_vector=[1.0] * profile.dim, **arguments)
        assert_parity(binary, baseline)
    corpus.close()


def test_missing_artifact_falls_back(root, vectors):
    corpus, profile = vector_library(root)
    assert 'vector_cache_unavailable' in search(corpus, profile)['warnings']
    corpus.close()


@pytest.mark.parametrize('damage', ['length', 'bytes', 'schema', 'index', 'metadata', 'warm_bytes'])
def test_corrupt_artifact_falls_back(root, vectors, damage):
    corpus, profile = vector_library(root)
    expected = search(corpus, profile)
    result = vectors.build(corpus, profile.id, apply=True)
    data_path, metadata_path = vectors.artifact_paths(root, profile.id, result['fingerprint'])
    if damage == 'warm_bytes':
        search(corpus, profile)
    if damage in ('length', 'bytes', 'warm_bytes'):
        with data_path.open('r+b') as handle:
            if damage == 'length':
                handle.truncate(4)
            else:
                handle.write(struct.pack('<f', 777.0))
    elif damage == 'metadata':
        metadata_path.write_text('{')
    else:
        metadata = json.loads(metadata_path.read_text())
        metadata['schema' if damage == 'schema' else 'index_sha256'] = 'SYNTHETIC-invalid'
        metadata_path.write_text(encoded(metadata))
    actual = search(corpus, profile)
    assert 'vector_cache_corrupt' in actual['warnings']
    assert_parity(actual, expected)
    corpus.close()


@pytest.mark.parametrize('change', ['count', 'max_rowid', 'config'])
def test_stale_artifact_falls_back(root, vectors, change):
    corpus, profile = vector_library(root)
    vectors.build(corpus, profile.id, apply=True)
    if change == 'count':
        corpus.db.execute("DELETE FROM chunk_embeddings WHERE chunk_id='budget_000029'")
    elif change == 'max_rowid':
        corpus.db.execute("UPDATE chunk_embeddings SET rowid=rowid+100 WHERE chunk_id='budget_000029'")
    else:
        config = {**profile.config, 'preprocessing_version': 'SYNTHETIC-new'}
        corpus.db.execute('UPDATE embedding_profiles SET config=? WHERE id=?', (encoded(config), profile.id))
    expected = search(corpus, profile)
    assert 'vector_cache_stale' in expected['warnings']
    vectors.build(corpus, profile.id, apply=True)
    assert_parity(search(corpus, profile), expected)
    assert len(list((root / 'runtime' / 'vectors').glob('*.f32'))) == 2
    corpus.close()


def test_cache_bounded_and_evicts(root, vectors, monkeypatch):
    corpus, first = vector_library(root)
    second = Profile('mock', 'SYNTHETIC-second', 4)
    corpus.db.execute('INSERT INTO embedding_profiles VALUES (?,?)', (second.id, encoded(second.config)))
    corpus.db.execute('INSERT INTO chunk_embeddings SELECT chunk_id,?,vector FROM chunk_embeddings', (second.id,))
    for profile in (first, second):
        vectors.build(corpus, profile.id, apply=True)
    monkeypatch.setenv('CORPUS_VECTOR_CACHE_BYTES', str(30 * 4 * 4))
    for profile in (first, second, first):
        assert 'vector_binary_scan' in search(corpus, profile)['warnings']
        assert vectors.CACHE.resident_bytes == 480
        assert len(vectors.CACHE.entries) == 1
    corpus.close()


def test_cache_disabled_by_env(root, vectors, monkeypatch):
    corpus, profile = vector_library(root)
    expected = search(corpus, profile)
    vectors.build(corpus, profile.id, apply=True)
    search(corpus, profile)
    monkeypatch.setenv('CORPUS_VECTOR_CACHE_BYTES', '0')
    actual = search(corpus, profile)
    assert 'vector_binary_scan' not in actual['warnings']
    assert vectors.CACHE.resident_bytes == 0
    assert_parity(actual, expected)
    corpus.close()


def test_artifact_path_outside_root_rejected(root, vectors):
    with pytest.raises(CorpusError, match='path'):
        vectors.artifact_paths(root, '../../../../escape', 'invalid')


def test_concurrent_read_during_rebuild(root, vectors, monkeypatch):
    corpus, profile = vector_library(root)
    result = vectors.build(corpus, profile.id, apply=True)
    data_path, metadata_path = vectors.artifact_paths(root, profile.id, result['fingerprint'])
    assert 'vector_binary_scan' in search(corpus, profile)['warnings']
    old_matrix = next(iter(vectors.CACHE.entries.values()))
    original_replace = vectors.os.replace
    observations = []

    def observed_replace(source, target):
        with ThreadPoolExecutor(max_workers=1) as pool:
            observations.append(pool.submit(lambda: (data_path.stat().st_size,
                json.loads(metadata_path.read_text())['dim'],
                old_matrix.score([1.0] * 4, 2.0, 'budget_000000'))).result())
        original_replace(source, target)

    monkeypatch.setattr(vectors.os, 'replace', observed_replace)
    vectors.build(corpus, profile.id, apply=True, force=True)
    assert observations and all(item[:2] == (30 * 4 * 4, 4) and math.isfinite(item[2]) for item in observations)
    assert 'vector_binary_scan' in search(corpus, profile)['warnings']
    corpus.close()


def test_kernel_is_swappable(root, vectors, monkeypatch):
    corpus, profile = vector_library(root)
    vectors.build(corpus, profile.id, apply=True)
    expected = corpus.search('absent', profile=profile, query_vector=[1.0] * profile.dim)
    calls = []

    def replacement(query_vector, matrix_view, row_index):
        calls.append(row_index)
        return 10000.0 if row_index == 29 else 0.0

    monkeypatch.setattr(vectors, 'dot_kernel', replacement)
    actual = corpus.search('absent', profile=profile, query_vector=[1.0] * profile.dim)
    assert len(calls) == 30
    assert actual['hits'][0]['ref'] != expected['hits'][0]['ref']
    assert actual['hits'][0]['ref'].endswith('budget_000029')
    corpus.close()


def test_kernel_failure_falls_back(root, vectors, monkeypatch):
    corpus, profile = vector_library(root)
    expected = search(corpus, profile)
    vectors.build(corpus, profile.id, apply=True)

    def failure(*args):
        raise RuntimeError('SYNTHETIC kernel failure')

    monkeypatch.setattr(vectors, 'dot_kernel', failure)
    actual = search(corpus, profile)
    assert 'vector_cache_corrupt' in actual['warnings']
    assert_parity(actual, expected)
    corpus.close()


def test_verify_detects_same_count_replacement(root, vectors):
    corpus, profile = vector_library(root)
    vectors.build(corpus, profile.id, apply=True)
    corpus.db.execute('UPDATE chunk_embeddings SET vector=? WHERE chunk_id=?', (encoded([9.0] * 4), 'budget_000000'))
    assert vectors.verify(corpus, profile.id)['ok'] is False
    corpus.close()


def test_no_network(root, vectors, capsys):
    from corpus_core.cli import main
    corpus, profile = vector_library(root)
    assert os.environ['CORPUS_OFFLINE'] == '1'
    arguments = ['--root', str(root), 'vectors-build', '--profile-id', profile.id]
    assert main(arguments) == 0
    assert not (root / 'runtime' / 'vectors').exists()
    assert main(arguments + ['--apply']) == 0
    assert main(['--root', str(root), 'vectors-verify', '--profile-id', profile.id]) == 0
    assert 'vector_binary_scan' in search(corpus, profile)['warnings']
    assert '"ok": true' in capsys.readouterr().out
    corpus.close()


def test_timings_are_opt_in(root, vectors, monkeypatch):
    corpus, profile = vector_library(root)
    monkeypatch.delenv('CORPUS_VECTOR_TIMINGS', raising=False)
    first = search(corpus, profile)
    assert search(corpus, profile) == first
    assert 'vector_scan_timings' not in first['retrieval_diagnostics']
    monkeypatch.setenv('CORPUS_VECTOR_TIMINGS', '1')
    measured = search(corpus, profile)
    assert measured['retrieval_diagnostics']['vector_scan_timings']['kernel_seconds'] > 0
    corpus.close()
