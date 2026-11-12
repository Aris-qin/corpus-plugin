import json

import pytest

from corpus_core.contracts import CONTRACTS, schema_for, schema_union, validate
from corpus_core.errors import CorpusError
from test_contract_matrix import CASES, example


@pytest.mark.parametrize('tool', CONTRACTS)
def test_flat_tool_schema(tool):
    schema = schema_for(tool)
    parameters = schema['parameters']
    assert parameters['type'] == 'object' and parameters['additionalProperties'] is False
    assert 'oneOf' not in parameters and 'properties' in parameters
    tag = next(iter(CONTRACTS[tool].values()))['tag']
    assert parameters['required'] == [tag]
    assert set(parameters['properties'][tag]['enum']) == set(CONTRACTS[tool])
    for action, specification in CONTRACTS[tool].items():
        assert set(specification['required']) <= set(parameters['properties'])
        line = next(line for line in schema['description'].splitlines() if line.startswith(f'{tag}={action}:'))
        assert 'required:' in line and 'optional:' in line
        assert all(field in line for field in {**specification['required'], **specification['optional']})
    assert json.loads(json.dumps(parameters)) == parameters
    assert '$ref' not in json.dumps(parameters)


def test_nested_batch_oneof_preserved():
    parameters = schema_for('corpus_reference')['parameters']
    assert len(parameters['properties']['items']['items']['oneOf']) == 3


@pytest.mark.parametrize('tool,action,spec', CASES)
def test_strict_validation_still_rejects_bad_combinations(tool, action, spec):
    payload = {spec['tag']: action, **{field: example(schema) for field, schema in spec['required'].items()}}
    assert validate(tool, payload) == action
    bad = [{**payload, 'unexpected': True}, {**payload, spec['tag']: 'not-an-action'}, {**payload, spec['tag']: 1}]
    bad.extend({key: value for key, value in payload.items() if key != field} for field in spec['required'])
    bad.extend({**payload, field: None} for field in {**spec['required'], **spec['optional']})
    other_fields = set(schema_for(tool)['parameters']['properties']) - set(payload) - set(spec['optional'])
    bad.extend({**payload, field: 'SYNTHETIC'} for field in other_fields)
    for arguments in bad:
        with pytest.raises(CorpusError) as caught:
            validate(tool, arguments)
        assert caught.value.code == 'INVALID_ARGUMENT'


def test_parameter_bounds_union():
    assert schema_union({'type': 'string', 'maxLength': 5}, {'type': 'string', 'maxLength': 10})['maxLength'] == 10
    assert schema_union({'type': 'integer', 'minimum': 1, 'maximum': 5},
                        {'type': 'integer', 'minimum': 0, 'maximum': 10}) == {'type': 'integer', 'minimum': 0, 'maximum': 10}


def test_json_schema_metaschema():
    from jsonschema import Draft202012Validator
    for tool in CONTRACTS:
        Draft202012Validator.check_schema(schema_for(tool)['parameters'])


def test_host_adapter_contracts_match_candidate():
    from pathlib import Path
    source = Path(__file__).resolve().parents[2]
    adapter = source / 'plugins/hermes-corpus-query/corpus_core/contracts.py'
    if not adapter.is_file():
        pytest.skip('host adapter tree is not shipped in this artifact form')
    assert (source / 'corpus_core/contracts.py').read_bytes() == adapter.read_bytes()
