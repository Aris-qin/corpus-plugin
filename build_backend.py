import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parent
PROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
NAME = PROJECT['name'].replace('-', '_')
VERSION = PROJECT['version']


def output_directory(path, config_settings=None):
    configured = (config_settings or {}).get('sandbox_root') or os.environ.get('CORPUS_AUTHORIZED_SANDBOX_ROOT')
    if not configured:
        raise ValueError('explicit authorized sandbox_root build setting or launcher environment required')
    boundary = Path(configured).resolve(strict=True)
    if boundary == Path('/') or not ROOT.is_relative_to(boundary):
        raise ValueError('build source must be within explicit sandbox')
    directory = Path(path).resolve()
    if not directory.is_relative_to(boundary):
        raise ValueError('build output must stay inside authorized candidate boundary')
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    directory = output_directory(wheel_directory, config_settings)
    filename = f'{NAME}-{VERSION}-py3-none-any.whl'
    info = f'{NAME}-{VERSION}.dist-info'
    entries = {str(path.relative_to(ROOT)): path.read_bytes() for path in sorted((ROOT / 'corpus_core').glob('*.py'))}
    entries[info + '/METADATA'] = f'Metadata-Version: 2.1\nName: {PROJECT["name"]}\nVersion: {VERSION}\nSummary: {PROJECT["description"]}\nRequires-Python: >=3.11\n'.encode()
    entries[info + '/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: corpus-stdlib-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    entries[info + '/entry_points.txt'] = b'[console_scripts]\ncorpus = corpus_core.cli:main\n'
    for filename_source in ('plugin.yaml', '__init__.py'):
        entries[f'{NAME}-{VERSION}.data/data/hermes-plugins/corpus-candidate/{filename_source}'] = (ROOT / filename_source).read_bytes()
    for path in sorted((ROOT / 'corpus_core').glob('*.py')):
        entries[f'{NAME}-{VERSION}.data/data/hermes-plugins/corpus-candidate/corpus_core/{path.name}'] = path.read_bytes()
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, data in sorted(entries.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
        writer.writerow([name, 'sha256=' + checksum, len(data)])
    writer.writerow([info + '/RECORD', '', ''])
    entries[info + '/RECORD'] = record.getvalue().encode()
    with zipfile.ZipFile(directory / filename, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 11, 10, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    return filename


def build_sdist(sdist_directory, config_settings=None):
    directory = output_directory(sdist_directory, config_settings)
    filename = f'{NAME}-{VERSION}.tar.gz'
    paths = []
    for pattern in ('*.md', '*.json', '*.py', '*.toml', '*.yaml', '*.sh', 'corpus_core/*.py', 'tests/safe/*.py', 'docs/**/*.md', 'docs/**/*.json', 'tools/*.sh', 'tools/*.py', 'plugins/hermes-corpus-query/*.py', 'plugins/hermes-corpus-query/*.yaml', 'plugins/hermes-corpus-query/corpus_core/*.py'):
        paths.extend(ROOT.glob(pattern))
    with tarfile.open(directory / filename, 'w:gz') as archive:
        for path in sorted(set(paths)):
            archive.add(path, arcname=f'{NAME}-{VERSION}/' + str(path.relative_to(ROOT)), recursive=False)
    return filename


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    print(json.dumps({'wheel': build_wheel(args.out), 'sdist': build_sdist(args.out)}))
