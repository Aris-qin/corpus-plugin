import json

import pytest

from corpus_core.contracts import CONTRACTS, validate
from corpus_core.errors import CorpusError


CASES = [(tool, name, spec) for tool, actions in CONTRACTS.items() for name, spec in actions.items()]


def example(schema):
    if 'oneOf' in schema:
        return example(schema['oneOf'][0])
    if 'enum' in schema:
        return schema['enum'][0]
    kind = schema['type']
    if kind == 'string':
        return 'SYNTHETIC'
    if kind in ('integer', 'number'):
        return max(1, schema.get('minimum', 0))
    if kind == 'object':
        fields = schema.get('required', [])
        if len(fields) < schema.get('minProperties', 0):
            fields = list(schema['properties'])[:schema['minProperties']]
        return {field: example(schema['properties'][field]) for field in fields}
    if kind == 'array':
        return [example(schema['items']) for index in range(schema.get('minItems', 0))]


@pytest.mark.parametrize('tool,name,spec', CASES, ids=[tool + ':' + name for tool, name, spec in CASES])
def test_every_action_required_forbidden_and_type_contracts(tool, name, spec):
    payload = {spec['tag']: name, **{field: example(schema) for field, schema in spec['required'].items()}}
    assert validate(tool, payload) == name
    with pytest.raises(CorpusError):
        validate(tool, {**payload, 'confirm': True})
    for field in spec['required']:
        with pytest.raises(CorpusError):
            validate(tool, {key: value for key, value in payload.items() if key != field})
    for field, schema in {**spec['required'], **spec['optional']}.items():
        if schema.get('type') == 'integer':
            for wrong in (True, '2', -1, 1000000001):
                with pytest.raises(CorpusError):
                    validate(tool, {**payload, field: wrong})


def test_history_is_only_paginated_read_contract():
    with pytest.raises(CorpusError):
        validate('corpus_read', {'view': 'chunk', 'ref': 'corpus://chunk/SYNTHETIC', 'limit': 2})


def test_preferred_formal_does_not_regress_to_preprint(root):
    from corpus_core.service import Corpus
    from test_retrieval import seed
    corpus = Corpus(root)
    original = seed(corpus)
    def add(kind, revision, operation):
        return corpus.ingest({'inputs': [{'source': 'text', 'content': 'SYNTHETIC ' + operation, 'title': 'SYNTHETIC version',
                                         'version_kind': kind, 'work_id': original['work_id'], 'expected_revision': revision,
                                         'relationship_source': 'corpus://version/' + original['version_id']}], 'operation_id': operation})['items'][0]
    formal = add('formal', 1, 'formal')
    add('preprint', 2, 'late-preprint')
    assert corpus.read('corpus://work/' + original['work_id'])['version']['id'] == formal['version_id']


def test_packaged_wheel_runs_outside_source(root):
    import os
    import subprocess
    import sys
    import zipfile
    import build_backend
    root.mkdir(parents=True)
    wheel = build_backend.build_wheel(str(root))
    unpacked = root / 'installed'
    with zipfile.ZipFile(root / wheel) as archive:
        archive.extractall(unpacked)
    env = {**os.environ, 'PYTHONPATH': str(unpacked)}
    result = subprocess.run([sys.executable, '-m', 'corpus_core.cli', '--root', str(root / 'data'), 'probe'],
                            cwd=unpacked, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['fts5']
