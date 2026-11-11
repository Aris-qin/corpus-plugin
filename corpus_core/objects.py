import hashlib
import os
import tempfile

from .errors import require
from .paths import bounded


def verified_blob(root, blob):
    path = bounded(root / blob['path'], root)
    require(path.is_file(), 'OBJECT_CORRUPT', 'preserved original missing')
    content = path.read_bytes()
    require(len(content) == blob['size'] and hashlib.sha256(content).hexdigest() == blob['hash'],
            'OBJECT_CORRUPT', 'preserved original hash/size mismatch')
    return path


def publish(root, content):
    content_hash = hashlib.sha256(content).hexdigest()
    destination = bounded(root / 'objects' / 'sha256' / content_hash[:2] / content_hash, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging = tempfile.mkstemp(prefix='.stage-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staging, destination)
        except FileExistsError:
            require(hashlib.sha256(destination.read_bytes()).hexdigest() == content_hash,
                    'OBJECT_CORRUPT', 'existing object hash mismatch')
        directory = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(staging)
    return {'hash': content_hash, 'size': len(content), 'path': str(destination.relative_to(root))}


def fsck(corpus):
    known = {row['hash']: row for row in corpus.db.execute('SELECT * FROM blobs')}
    problems = []
    for content_hash, row in known.items():
        path = bounded(corpus.root / row['path'], corpus.root)
        if not path.is_file() or path.stat().st_size != row['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != content_hash:
            problems.append({'hash': content_hash, 'issue': 'missing_or_corrupt'})
    referenced = set(known)
    referenced.update(row['source_hash'] for row in corpus.db.execute('SELECT source_hash FROM migrations'))
    objects = bounded(corpus.root / 'objects' / 'sha256', corpus.root)
    orphans = [str(path.relative_to(corpus.root)) for path in objects.glob('*/*') if path.is_file() and path.name not in referenced]
    return {'problems': problems, 'unreferenced_or_derived_objects': orphans, 'deleted': 0}


def atomic_export(root, destination, content, replace=False):
    destination = bounded(destination, root)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.export-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError:
                require(destination.read_bytes() == content, 'EXPORT_DRIFT', 'export was edited; import manually as a new draft')
        directory = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
