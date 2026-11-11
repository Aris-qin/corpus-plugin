import os
from pathlib import Path

from .errors import require


def sandbox_root(explicit=None):
    configured = explicit or os.environ.get('CORPUS_AUTHORIZED_SANDBOX_ROOT')
    require(configured is not None, 'ROOT_REQUIRED', 'explicit authorized sandbox root required')
    root = Path(configured)
    require(root.is_absolute() and root.is_dir(), 'UNSAFE_PATH', 'sandbox root must be an existing absolute directory')
    require(root.resolve() != Path('/'), 'UNSAFE_PATH', 'filesystem root is not a sandbox')
    return root.resolve()


def bounded(path, root=None):
    root = sandbox_root() if root is None else Path(root)
    lexical = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    require(root.resolve() == root, 'UNSAFE_PATH', 'containment root was replaced by a symlink')
    require(lexical.is_relative_to(root), 'UNSAFE_PATH', 'path escapes explicit root')
    resolved = lexical.resolve()
    require(resolved.is_relative_to(root.resolve()), 'UNSAFE_PATH', 'symlink escapes explicit root')
    return resolved


def data_root(path, authorized_root=None):
    require(path is not None, 'ROOT_REQUIRED', 'explicit candidate root required')
    boundary = sandbox_root(authorized_root)
    resolved = bounded(path, boundary)
    require(resolved != boundary, 'UNSAFE_PATH', 'use a dedicated data directory')
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
