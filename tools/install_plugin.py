"""Explicit installation only; never migrate, configure or restart Hermes."""
import argparse
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--mode', choices=['direct', 'adapter'], default='direct')
    args = parser.parse_args()
    target = args.target
    if not target.is_absolute() or target.exists() or target.is_symlink():
        parser.error('target must be a new explicit absolute directory; back up existing installation first')
    root = Path(__file__).resolve().parent.parent
    source = root if args.mode == 'direct' else root / 'plugins' / 'hermes-corpus-query'
    # Fail before creating target if source is incomplete.
    names = ['__init__.py', 'plugin.yaml', 'corpus_core']
    if args.mode == 'adapter':
        names.append('_bridge.py')
    if not all((source / name).exists() for name in names):
        parser.error('incomplete source tree')
    target.mkdir(parents=True, exist_ok=False)
    try:
        for name in names:
            item = source / name
            if item.is_dir():
                shutil.copytree(item, target / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(item, target / name)
    except BaseException:
        shutil.rmtree(target)
        raise
    print(f'Installed {args.mode} plugin at {target}; configure explicitly before loading. No restart performed.')


if __name__ == '__main__':
    main()
