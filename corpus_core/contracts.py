from .errors import CorpusError, require
import math


STRING = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
OPERATION = {**STRING, 'maxLength': 200}
INTEGER = {'type': 'integer', 'minimum': 0, 'maximum': 1000000000}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 100}
OBJECT = {'type': 'object', 'maxProperties': 100}
STRINGS = {'type': 'array', 'items': STRING, 'maxItems': 100}
INPUT = {'type': 'object', 'required': ['source', 'title'], 'additionalProperties': False,
         'properties': {'source': {'enum': ['text', 'file', 'url', 'identifier']}, 'title': {**STRING, 'maxLength': 1000},
                        'content': {**STRING, 'maxLength': 10000000}, 'path': STRING, 'url': STRING, 'identifier': STRING,
                        'media_type': {'enum': ['text/plain', 'text/markdown', 'text/html', 'application/pdf']},
                        'coverage': {'enum': ['unknown', 'abstract', 'fulltext']},
                        'contexts': STRINGS, 'kind': {'enum': ['paper', 'web', 'guideline', 'template', 'other']},
                        'work_id': STRING, 'expected_revision': INTEGER,
                        'version_kind': {'enum': ['preprint', 'formal', 'snapshot', 'correction', 'supplement']},
                        'relationship_source': STRING,
                        'identifiers': {'type': 'array', 'maxItems': 20, 'items': {'type': 'object', 'additionalProperties': False,
                            'required': ['scheme', 'raw_value', 'verification_source'], 'properties': {
                                'scheme': {'enum': ['doi', 'pmid', 'pmcid', 'url']}, 'raw_value': STRING, 'verification_source': STRING}}}}}
INPUTS = {'type': 'array', 'items': INPUT, 'minItems': 1, 'maxItems': 10}
METADATA = {'type': 'object', 'additionalProperties': False, 'minProperties': 1, 'properties': {
    'title': {**STRING, 'maxLength': 1000},
    'year': {'type': 'integer', 'minimum': 1000, 'maximum': 9999},
    'authors': {'type': 'array', 'minItems': 1, 'maxItems': 1000, 'items': {**STRING, 'maxLength': 500}},
    'doi': STRING, 'retraction_status': {'enum': ['unknown', 'not_retracted', 'retracted', 'corrected', 'expression_of_concern']}}}
FILTERS = {'type': 'object', 'additionalProperties': False, 'properties': {
    **{field: STRINGS for field in ('work_ids', 'version_ids', 'version_kinds', 'kinds', 'coverage', 'retraction_status')},
    'year_min': {'type': 'integer', 'minimum': 1000, 'maximum': 9999},
    'year_max': {'type': 'integer', 'minimum': 1000, 'maximum': 9999}, 'doi': STRING}}
ASSESSMENT = {'type': 'object', 'additionalProperties': False, 'minProperties': 1, 'properties': {
    'score': {'type': 'number', 'minimum': 0, 'maximum': 100}, 'formula': STRING, 'uncertainty': STRING, 'basis': STRING}}


def action(required, optional=None, tag='action'):
    return {'tag': tag, 'required': required, 'optional': optional or {}}


APPLY = action({'proposal_id': STRING, 'approval_ref': STRING, 'operation_id': OPERATION})
PROPOSE = action({'revision_id': STRING, 'operation_id': OPERATION})
READ_OPTIONS = {'use_context': {'enum': ['browse', 'report', 'submission']}, 'limit': LIMIT, 'cursor': STRING}
CONTRACTS = {
    'corpus_ingest': {
        'preflight': action({'inputs': INPUTS}),
        'submit': action({'inputs': INPUTS, 'operation_id': OPERATION}),
    },
    'corpus_search': {
        'overview': action({'context': STRING}, {'limit': LIMIT, 'cursor': STRING}, 'mode'),
        'evidence': action({'query': STRING}, {'scope': {'enum': ['global', 'linked']}, 'context': STRING, 'top_k': LIMIT, 'cursor': STRING, 'filters': FILTERS, 'candidate_budget': {'type': 'integer', 'minimum': 1, 'maximum': 10000}}, 'mode'),
        'similar': action({'ref': STRING}, {'top_k': LIMIT, 'cursor': STRING}, 'mode'),
        'research': action({'query': STRING}, {'context': STRING, 'limit': LIMIT, 'cursor': STRING}, 'mode'),
    },
    'corpus_read': {view: action({'ref': STRING}, {'limit': LIMIT, 'cursor': STRING} if view == 'history' else {
        'use_context': READ_OPTIONS['use_context'], **({'before': {'type': 'integer', 'minimum': 0, 'maximum': 5}, 'after': {'type': 'integer', 'minimum': 0, 'maximum': 5}} if view == 'chunk' else {})}, 'view') for view in ('item', 'version', 'chunk', 'history', 'resolve')},
    'corpus_reference': {
        'save_draft': action({'kind': {'enum': ['topic_report', 'journal_profile', 'author_guidance', 'project_overview', 'submission_assessment', 'method_comparison']},
                              'body': {**STRING, 'maxLength': 1000000}, 'evidence': STRINGS, 'structured': OBJECT,
                              'change_summary': STRING, 'reason': STRING, 'operation_id': OPERATION},
                             {'record_id': STRING, 'parent_id': STRING, 'expected_revision': INTEGER}),
        'propose': PROPOSE,
        'propose_batch': action({'items': {'type': 'array', 'items': OBJECT, 'minItems': 1, 'maxItems': 10}, 'operation_id': OPERATION}),
        'apply': APPLY,
        'reject': action({'proposal_id': STRING, 'operation_id': OPERATION}),
    },
    'corpus_manage': {
        'metadata': action({'target': STRING, 'expected_revision': INTEGER, 'changes': METADATA, 'source_ref': STRING, 'operation_id': OPERATION}),
        'link': action({'target': STRING, 'context': STRING, 'operation_id': OPERATION}),
        'external_link': action({'target': STRING, 'uri': STRING, 'basis': STRING, 'operation_id': OPERATION}),
        'assess': action({'target': STRING, 'expected_revision': INTEGER, 'assessment': ASSESSMENT, 'source_ref': STRING, 'operation_id': OPERATION}),
        'review': action({'target': STRING, 'scope': STRINGS, 'source_ref': STRING, 'conclusion': STRING, 'remaining': STRINGS, 'operation_id': OPERATION}),
        'propose': action({'maintenance': {'enum': ['merge', 'purge'], 'description': 'purge explicitly proposes permanent owned-content deletion of a recycled work; known references, aliases, shared bytes and active jobs block it; no secure erasure'}, 'target': STRING, 'expected_revision': INTEGER, 'reason': STRING, 'operation_id': OPERATION}, {'merge_into': STRING, 'merge_expected_revision': INTEGER}),
        'apply': APPLY,
        'recycle': action({'target': STRING, 'expected_revision': INTEGER, 'operation_id': OPERATION}),
        'restore': action({'target': STRING, 'expected_revision': INTEGER, 'operation_id': OPERATION}),
    },
    'corpus_jobs': {
        'status': action({'job_id': STRING}),
        'list': action({}, {'state': {'enum': ['queued', 'running', 'failed', 'cancelled', 'partial', 'succeeded']}, 'limit': LIMIT, 'cursor': STRING}),
        'retry': action({'job_id': STRING, 'operation_id': OPERATION}),
        'cancel': action({'job_id': STRING, 'operation_id': OPERATION}),
    },
}

DRAFT_SPEC = CONTRACTS['corpus_reference']['save_draft']
DRAFT_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': list(DRAFT_SPEC['required']),
                'properties': {**DRAFT_SPEC['required'], **DRAFT_SPEC['optional']}}
CONTRACTS['corpus_reference']['propose_batch']['required']['items']['items'] = {'oneOf': [
    {'type': 'object', 'additionalProperties': False, 'required': ['action', 'input'],
     'properties': {'action': {'enum': ['save_source']}, 'input': INPUT}},
    {'type': 'object', 'additionalProperties': False, 'required': ['action', 'draft'],
     'properties': {'action': {'enum': ['save_draft']}, 'draft': DRAFT_SCHEMA}},
    {'type': 'object', 'additionalProperties': False, 'required': ['action', 'revision_id'],
     'properties': {'action': {'enum': ['publish_reference']}, 'revision_id': STRING}}]}


def validate_value(value, schema, path):
    if 'oneOf' in schema:
        matches = 0
        for alternative in schema['oneOf']:
            try:
                validate_value(value, alternative, path)
                matches += 1
            except CorpusError:
                pass
        require(matches == 1, 'INVALID_ARGUMENT', path + ' must match exactly one strict nested action')
    if 'enum' in schema:
        require(value in schema['enum'] and type(value) is str, 'INVALID_ARGUMENT', path + ' invalid enum')
    kind = schema.get('type')
    types = {'string': str, 'integer': int, 'object': dict, 'array': list, 'boolean': bool}
    if kind:
        if kind == 'number':
            require(type(value) in (int, float) and math.isfinite(value), 'INVALID_ARGUMENT', path + ' must be a finite number')
        else:
            require(type(value) is types[kind], 'INVALID_ARGUMENT', path + ' has wrong type')
    if kind == 'string':
        require(schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 1000000), 'INVALID_ARGUMENT', path + ' length out of bounds')
    if kind in ('integer', 'number'):
        require(schema.get('minimum', 0) <= value <= schema.get('maximum', 1000000000), 'INVALID_ARGUMENT', path + ' out of bounds')
    if kind == 'array':
        require(schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 100), 'INVALID_ARGUMENT', path + ' count out of bounds')
        for index, item in enumerate(value):
            validate_value(item, schema['items'], f'{path}[{index}]')
    if kind == 'object':
        require(schema.get('minProperties', 0) <= len(value) <= schema.get('maxProperties', 100), 'INVALID_ARGUMENT', path + ' field count out of bounds')
        require(set(schema.get('required', [])) <= set(value), 'INVALID_ARGUMENT', path + ' missing required fields')
        if schema.get('additionalProperties') is False:
            require(set(value) <= set(schema['properties']), 'INVALID_ARGUMENT', path + ' contains forbidden fields')
        for field, item in value.items():
            if field in schema.get('properties', {}):
                validate_value(item, schema['properties'][field], path + '.' + field)


def schema_union(left, right):
    if left == right:
        return dict(left)
    require(left.get('type') == right.get('type'), 'INVALID_SCHEMA', 'conflicting parameter types')
    result = {}
    lower_bounds = {'minimum', 'minLength', 'minItems', 'minProperties'}
    upper_bounds = {'maximum', 'maxLength', 'maxItems', 'maxProperties'}
    for key in left.keys() | right.keys():
        if key not in left or key not in right:
            if key in ('description', 'properties', 'items'):
                result[key] = left.get(key, right.get(key))
            continue
        first, second = left[key], right[key]
        if first == second:
            result[key] = first
        elif key in lower_bounds:
            result[key] = min(first, second)
        elif key in upper_bounds:
            result[key] = max(first, second)
        elif key == 'enum':
            result[key] = list(dict.fromkeys([*first, *second]))
        elif key == 'required':
            result[key] = [field for field in first if field in second]
        elif key == 'additionalProperties':
            require(type(first) is bool and type(second) is bool, 'INVALID_SCHEMA', 'unsupported object conflict')
            result[key] = first or second
        elif key == 'properties':
            result[key] = {**first, **second}
            for field in first.keys() & second.keys():
                result[key][field] = schema_union(first[field], second[field])
        elif key == 'items':
            result[key] = schema_union(first, second)
        elif key == 'description':
            result[key] = first + '; ' + second
        else:
            require(False, 'INVALID_SCHEMA', 'unsupported parameter conflict: ' + key)
    return result


def schema_for(name):
    actions = CONTRACTS[name]
    tag = next(iter(actions.values()))['tag']
    properties = {tag: {'type': 'string', 'enum': list(actions)}}
    action_descriptions = []
    for name_action, specification in actions.items():
        for field, schema in {**specification['required'], **specification['optional']}.items():
            properties[field] = schema_union(properties[field], schema) if field in properties else dict(schema)
        required = ', '.join(specification['required']) or '(none)'
        optional = ', '.join(specification['optional']) or '(none)'
        action_descriptions.append(f'{tag}={name_action}: required: {required}; optional: {optional}')
    description = name.replace('_', ' ') + '; candidate-only, strict per-action contracts'
    if name == 'corpus_manage':
        description += '; sensitive apply disabled by default: no trusted human approval adapter; isolated simulations are not identity proof'
    if name == 'corpus_jobs':
        description += '; partial means lexical/read available but vector index incomplete; retry uses explicit settings budget for original operation/profile; ambiguous billed results require local operator reconcile-embedding, never blind retry'
    if name == 'corpus_reference':
        description += '; drafts and proposals available; formal publication and batch apply disabled without trusted human approval integration'
    return {'name': name, 'description': description + '\n' + '\n'.join(action_descriptions),
            'parameters': {'type': 'object', 'additionalProperties': False,
                           'required': [tag], 'properties': properties}}


def validate(name, arguments):
    require(name in CONTRACTS and type(arguments) is dict, 'INVALID_ARGUMENT', 'unknown tool or non-object arguments')
    tag = next(iter(CONTRACTS[name].values()))['tag']
    action_name = arguments.get(tag)
    require(type(action_name) is str and action_name in CONTRACTS[name], 'INVALID_ARGUMENT', 'missing or unknown action tag')
    specification = CONTRACTS[name][action_name]
    schema = {'type': 'object', 'additionalProperties': False,
              'required': [tag, *specification['required']],
              'properties': {tag: {'enum': [action_name]}, **specification['required'], **specification['optional']}}
    validate_value(arguments, schema, name)
    return action_name
