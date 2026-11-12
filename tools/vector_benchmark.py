import argparse
import hashlib
import json
import math
import os
import resource
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

from corpus_core.db import encoded
from corpus_core.embeddings import Profile, cosine
from corpus_core.paths import bounded, sandbox_root
from corpus_core.service import Corpus
from corpus_core.vector_cache import CACHE, build, verify


def open_copy(root):
    boundary = sandbox_root()
    root = bounded(Path(root), boundary)
    require_copy = bounded(boundary / 'tmp', boundary)
    if not root.is_relative_to(require_copy) or os.environ.get('CORPUS_OFFLINE') != '1':
        raise ValueError('offline copy beneath sandbox tmp required')
    corpus = Corpus.__new__(Corpus)
    corpus.root = root
    corpus.sandbox_root = boundary
    corpus.test_mode = False
    corpus.embedding = None
    corpus.rerank = None
    path = bounded(root / 'catalogue.sqlite', root)
    corpus.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    corpus.db.row_factory = sqlite3.Row
    return corpus


def profile_query(corpus):
    row = corpus.db.execute('SELECT p.id,p.config,e.chunk_id,e.vector FROM embedding_profiles p JOIN chunk_embeddings e ON p.id=e.profile_id ORDER BY p.id,e.chunk_id LIMIT 1').fetchone()
    return Profile(**json.loads(row['config'])), row['chunk_id'], json.loads(row['vector'])


def hwm_kib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def worker(root, mode, repeats):
    corpus = open_copy(root)
    profile, chunk_id, query_vector = profile_query(corpus)
    os.environ['CORPUS_VECTOR_CACHE_BYTES'] = '0' if mode == 'json' else '268435456'
    CACHE.clear()
    os.environ['CORPUS_VECTOR_TIMINGS'] = '1'
    before = hwm_kib()
    samples = []
    for ordinal in range(repeats):
        started = time.perf_counter()
        result = corpus.search('vector measurement', profile=profile, query_vector=query_vector,
                               candidate_budget=10000, top_k=10)
        total = time.perf_counter() - started
        diagnostics = result['retrieval_diagnostics']
        timings = diagnostics['vector_scan_timings']
        sample = {**timings, 'lexical_scan_seconds': diagnostics['lexical_scan_seconds'],
                  'fusion_seconds': diagnostics['fusion_seconds'], 'total_seconds': total,
                  'vectors_scanned': diagnostics['vectors_scanned'], 'warnings': result['warnings'],
                  'cache_state': 'disabled' if mode == 'json' else ('cold' if ordinal == 0 else 'hot')}
        accounted = sum(value for key, value in sample.items() if key.endswith('_seconds') and key != 'total_seconds')
        sample['other_seconds'] = total - accounted
        samples.append(sample)
    after = hwm_kib()
    corpus.close()
    CACHE.clear()
    return {'mode': mode, 'query_chunk_id': chunk_id, 'query_text': 'vector measurement',
            'rerank_enabled': False, 'candidate_budget': 10000, 'top_k': 10,
            'memory_source': 'getrusage(RUSAGE_SELF).ru_maxrss; Linux RSS high-water mark, no /proc reads',
            'ru_maxrss_before_KiB': before, 'ru_maxrss_after_KiB': after, 'ru_maxrss_increment_KiB': after - before,
            'samples': samples}


def inversions(sequence):
    tree = [0] * (len(sequence) + 1)
    total = 0
    for seen, value in enumerate(sequence):
        index = value + 1
        prefix = 0
        while index:
            prefix += tree[index]
            index -= index & -index
        total += seen - prefix
        index = value + 1
        while index < len(tree):
            tree[index] += 1
            index += index & -index
    return total


def precision(corpus, profile, query_vector):
    baseline = {}
    binary = {}
    json_kernel_seconds = 0.0
    binary_kernel_seconds = 0.0
    with CACHE.open(corpus, profile.id) as (matrix, warning):
        if matrix is None:
            raise RuntimeError(warning)
        norm = math.hypot(*query_vector)
        for row in corpus.db.execute('SELECT chunk_id,vector FROM chunk_embeddings WHERE profile_id=? ORDER BY chunk_id', (profile.id,)):
            chunk_id = row['chunk_id']
            started = time.perf_counter()
            baseline[chunk_id] = cosine(query_vector, json.loads(row['vector']))
            json_kernel_seconds += time.perf_counter() - started
            started = time.perf_counter()
            binary[chunk_id] = matrix.score(query_vector, norm, chunk_id)
            binary_kernel_seconds += time.perf_counter() - started
    json_order = sorted(baseline, key=lambda chunk_id: (-baseline[chunk_id], chunk_id))
    binary_order = sorted(binary, key=lambda chunk_id: (-binary[chunk_id], chunk_id))
    positions = {chunk_id: ordinal for ordinal, chunk_id in enumerate(json_order)}
    top_json = json_order[:200]
    top_positions = {chunk_id: ordinal for ordinal, chunk_id in enumerate(top_json)}
    return {'count': len(baseline), 'full_json_kernel_seconds': json_kernel_seconds,
            'full_binary_kernel_seconds': binary_kernel_seconds, 'max_absolute_error': max(abs(binary[chunk_id] - baseline[chunk_id]) for chunk_id in baseline),
            'full_order_inversions': inversions([positions[chunk_id] for chunk_id in binary_order]),
            'top_200_identical': top_json == binary_order[:200],
            'top_200_internal_inversions': inversions([top_positions[chunk_id] for chunk_id in binary_order if chunk_id in top_positions]),
            'top_200_position_mismatches': sum(left != right for left, right in zip(top_json, binary_order[:200]))}


def database_sha(path):
    checksum = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def summary(worker_result):
    samples = worker_result['samples']
    result = {}
    for field in samples[0]:
        if field.endswith('_seconds'):
            values = sorted(sample[field] for sample in samples)
            result[field] = {'p50': statistics.median(values), 'p95': values[math.ceil(0.95 * len(values)) - 1]}
    return result


def measure(root, output, repeats=5):
    if repeats < 5:
        raise ValueError('at least five repeats required')
    corpus = open_copy(root)
    database = bounded(corpus.root / 'catalogue.sqlite', corpus.root)
    before = database_sha(database)
    profile, query_chunk, query_vector = profile_query(corpus)
    started = time.perf_counter()
    built = build(corpus, profile.id, apply=True, force=True)
    build_seconds = time.perf_counter() - started
    workers = {}
    for mode in ('json', 'binary'):
        command = [sys.executable, '-m', 'tools.vector_benchmark', '--root', str(corpus.root),
                   '--worker', mode, '--repeats', str(repeats)]
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        workers[mode] = json.loads(completed.stdout)
    os.environ['CORPUS_VECTOR_CACHE_BYTES'] = '268435456'
    CACHE.clear()
    accuracy = precision(corpus, profile, query_vector)
    verification = verify(corpus, profile.id)
    after = database_sha(database)
    summaries = {mode: summary(result) for mode, result in workers.items()}
    result = {'copy_root': str(corpus.root), 'profile_id': profile.id, 'query_chunk_id': query_chunk,
              'build': {**built, 'seconds': build_seconds}, 'workers': workers, 'summaries': summaries,
              'p50_speedup': summaries['json']['total_seconds']['p50'] / summaries['binary']['total_seconds']['p50'],
              'precision': accuracy, 'verification': verification, 'database_sha256_before': before,
              'database_sha256_after': after, 'database_unchanged': before == after,
              'percentile_method': 'p50=median; p95=nearest-rank, including process-cold first sample',
              'targets_met_p50': {'kernel_le_1_5s': summaries['binary']['kernel_seconds']['p50'] <= 1.5,
                                  'total_le_3s': summaries['binary']['total_seconds']['p50'] <= 3}}
    destination = bounded(Path(output), sandbox_root())
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    corpus.close()
    CACHE.clear()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--worker', choices=('json', 'binary'))
    parser.add_argument('--repeats', type=int, default=5)
    arguments = parser.parse_args()
    if not arguments.worker:
        parser.error('invoke measure through the isolated safe test')
    print(encoded(worker(arguments.root, arguments.worker, arguments.repeats)))


if __name__ == '__main__':
    main()
