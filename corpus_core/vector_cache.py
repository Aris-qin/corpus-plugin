import hashlib
import json
import math
import mmap
import operator
import os
import struct
import sys
import tempfile
import threading
from collections import OrderedDict
from contextlib import contextmanager

from .db import digest, encoded, now
from .embeddings import Profile
from .errors import require
from .paths import bounded


def fingerprint(corpus, profile_id):
    config_row = corpus.db.execute('SELECT config FROM embedding_profiles WHERE id=?', (profile_id,)).fetchone()
    require(config_row is not None, 'INVALID_PROFILE', 'indexed profile required')
    config = json.loads(config_row['config'])
    count, maximum = corpus.db.execute('SELECT count(*),max(rowid) FROM chunk_embeddings WHERE profile_id=?', (profile_id,)).fetchone()
    return digest({'count': count, 'max_rowid': maximum, 'config': digest(config)}), config, count


def artifact_paths(root, profile_id, identity):
    prefix = bounded(root / 'runtime' / 'vectors' / (profile_id + '-' + identity), root)
    return bounded(str(prefix) + '.f32', root), bounded(str(prefix) + '.json', root)


def index_rows(corpus, profile_id):
    indices = {}
    checksum = hashlib.sha256()
    for ordinal, row in enumerate(corpus.db.execute('SELECT chunk_id FROM chunk_embeddings WHERE profile_id=? ORDER BY chunk_id', (profile_id,))):
        indices[row['chunk_id']] = ordinal
        checksum.update((encoded([row['chunk_id'], ordinal]) + '\n').encode())
    return indices, checksum.hexdigest()


def packed_rows(corpus, profile_id, config):
    profile = Profile(**config)
    packer = struct.Struct('<' + 'f' * profile.dim)
    for row in corpus.db.execute('SELECT chunk_id,vector FROM chunk_embeddings WHERE profile_id=? ORDER BY chunk_id', (profile_id,)):
        vector = profile.vector(json.loads(row['vector']))
        packed = packer.pack(*vector)
        quantized = packer.unpack(packed)
        require(all(map(math.isfinite, quantized)) and math.hypot(*quantized) > 0,
                'INVALID_VECTOR', 'float32 vector is nonfinite or zero')
        yield row['chunk_id'], packed


def build(corpus, profile_id, apply=False, force=False):
    corpus.db.execute('SAVEPOINT vector_build')
    temporary = []
    try:
        identity, config, count = fingerprint(corpus, profile_id)
        data_path, metadata_path = artifact_paths(corpus.root, profile_id, identity)
        result = {'ok': True, 'dry_run': not apply, 'profile_id': profile_id, 'fingerprint': identity,
                  'count': count, 'dim': config['dim'], 'bytes': count * config['dim'] * 4,
                  'path': str(data_path)}
        if not apply:
            return result
        require(count > 0, 'INVALID_VECTOR', 'nonempty vector profile required')
        if not force and data_path.exists() and metadata_path.exists() and verify(corpus, profile_id)['ok']:
            return result
        bounded(data_path.parent, corpus.root).mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256()
        index_checksum = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=data_path.parent, suffix='.tmp', delete=False) as output:
            temporary.append(bounded(output.name, corpus.root))
            for ordinal, (chunk_id, packed) in enumerate(packed_rows(corpus, profile_id, config)):
                output.write(packed)
                checksum.update(packed)
                index_checksum.update((encoded([chunk_id, ordinal]) + '\n').encode())
            output.flush()
            os.fsync(output.fileno())
        require(temporary[0].stat().st_size == result['bytes'], 'INVALID_VECTOR', 'vector snapshot length changed')
        metadata = {'schema': 1, 'profile_id': profile_id, 'dim': config['dim'], 'count': count,
                    'dtype': 'float32-le', 'fingerprint': identity, 'created_at': now(),
                    'index_sha256': index_checksum.hexdigest(), 'data_sha256': checksum.hexdigest()}
        with tempfile.NamedTemporaryFile(dir=metadata_path.parent, suffix='.tmp', delete=False) as output:
            temporary.append(bounded(output.name, corpus.root))
            output.write(encoded(metadata).encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary[0], bounded(data_path, corpus.root))
        os.replace(temporary[1], bounded(metadata_path, corpus.root))
        return result
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
        corpus.db.execute('RELEASE vector_build')


class Matrix:
    def __init__(self, corpus, profile_id, identity, config, count):
        self.mapping = None
        self.view = None
        self.data_path, self.metadata_path = artifact_paths(corpus.root, profile_id, identity)
        try:
            metadata = json.loads(self.metadata_path.read_text())
            require(metadata['schema'] == 1 and metadata['dtype'] == 'float32-le'
                    and metadata['profile_id'] == profile_id and metadata['fingerprint'] == identity
                    and metadata['count'] == count and metadata['dim'] == config['dim'],
                    'VECTOR_CORRUPT', 'vector metadata mismatch')
            require(sys.byteorder == 'little', 'VECTOR_CORRUPT', 'native float view requires little endian')
            self.dim = config['dim']
            self.byte_size = count * self.dim * 4
            self.indices, index_checksum = index_rows(corpus, profile_id)
            require(index_checksum == metadata['index_sha256'] and len(self.indices) == count,
                    'VECTOR_CORRUPT', 'vector index mismatch')
            with self.data_path.open('rb') as source:
                require(os.fstat(source.fileno()).st_size == self.byte_size and self.byte_size > 0,
                        'VECTOR_CORRUPT', 'vector length mismatch')
                self.mapping = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)
            require(hashlib.sha256(self.mapping).hexdigest() == metadata['data_sha256'], 'VECTOR_CORRUPT', 'vector data hash mismatch')
            self.view = memoryview(self.mapping).cast('f')
            self.norms = []
            for ordinal in range(count):
                row = self.view[ordinal * self.dim:(ordinal + 1) * self.dim]
                try:
                    require(all(map(math.isfinite, row)), 'VECTOR_CORRUPT', 'nonfinite vector')
                    norm = math.hypot(*row)
                    require(norm > 0, 'VECTOR_CORRUPT', 'zero vector')
                    self.norms.append(norm)
                finally:
                    row.release()
            self.signature = self.file_signature()
        except BaseException:
            self.close()
            raise

    def file_signature(self):
        return tuple((path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
                     for path in (self.data_path, self.metadata_path))

    def score(self, query_vector, query_norm, chunk_id):
        row_index = self.indices[chunk_id]
        score = dot_kernel(query_vector, self.view, row_index) / (query_norm * self.norms[row_index])
        require(math.isfinite(score), 'VECTOR_CORRUPT', 'nonfinite kernel output')
        return score

    def close(self):
        if self.view is not None:
            self.view.release()
            self.view = None
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None


def dot_kernel(query_vector, matrix_view, row_index):
    dim = len(query_vector)
    row = matrix_view[row_index * dim:(row_index + 1) * dim]
    try:
        return sum(map(operator.mul, row, query_vector))
    finally:
        row.release()


def verify(corpus, profile_id):
    matrix = None
    try:
        identity, config, count = fingerprint(corpus, profile_id)
        matrix = Matrix(corpus, profile_id, identity, config, count)
        checksum = hashlib.sha256()
        for chunk_id, packed in packed_rows(corpus, profile_id, config):
            checksum.update(packed)
        metadata = json.loads(matrix.metadata_path.read_text())
        require(checksum.hexdigest() == metadata['data_sha256'], 'VECTOR_CORRUPT', 'database vector content changed')
        return {'ok': True, 'profile_id': profile_id, 'count': count, 'fingerprint': identity,
                'index_sha256': metadata['index_sha256']}
    except Exception as error:
        return {'ok': False, 'profile_id': profile_id, 'error': type(error).__name__ + ': ' + str(error)}
    finally:
        if matrix is not None:
            matrix.close()


class VectorCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.resident_bytes = 0
        self.lock = threading.RLock()

    def clear(self):
        with self.lock:
            while self.entries:
                self.evict()

    def evict(self):
        path, matrix = self.entries.popitem(last=False)
        self.resident_bytes -= matrix.byte_size
        matrix.close()

    @contextmanager
    def open(self, corpus, profile_id):
        with self.lock:
            matrix = None
            warning = 'vector_cache_unavailable'
            try:
                limit = int(os.environ.get('CORPUS_VECTOR_CACHE_BYTES', '268435456'))
                require(0 <= limit <= 4294967296, 'INVALID_BUDGET', 'vector cache bytes must be 0..4GiB')
                while self.entries and self.resident_bytes > limit:
                    self.evict()
                if limit:
                    identity, config, count = fingerprint(corpus, profile_id)
                    data_path, metadata_path = artifact_paths(corpus.root, profile_id, identity)
                    if not data_path.exists() or not metadata_path.exists():
                        directory = bounded(data_path.parent, corpus.root)
                        if data_path.exists() or metadata_path.exists():
                            warning = 'vector_cache_corrupt'
                        elif any(directory.glob(profile_id + '-*.json')):
                            warning = 'vector_cache_stale'
                    elif count * config['dim'] * 4 <= limit:
                        cached = self.entries.get(data_path)
                        if cached is not None and cached.signature != cached.file_signature():
                            self.resident_bytes -= cached.byte_size
                            del self.entries[data_path]
                            cached.close()
                            cached = None
                        if cached is None:
                            while self.entries and self.resident_bytes + count * config['dim'] * 4 > limit:
                                self.evict()
                            cached = Matrix(corpus, profile_id, identity, config, count)
                            self.entries[data_path] = cached
                            self.resident_bytes += cached.byte_size
                        self.entries.move_to_end(data_path)
                        matrix = cached
                        warning = 'vector_binary_scan'
            except Exception:
                warning = 'vector_cache_corrupt'
            yield matrix, warning


CACHE = VectorCache()
