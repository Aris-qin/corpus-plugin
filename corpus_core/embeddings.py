import math
from dataclasses import asdict, dataclass

from .db import digest
from .errors import require
from .paths import bounded


@dataclass(frozen=True)
class Profile:
    provider: str
    exact_model: str
    dim: int
    metric: str = 'cosine'
    preprocessing_version: str = 'canonical-v1'
    provenance: str = 'explicit_configuration'

    @property
    def config(self):
        return asdict(self)

    @property
    def id(self):
        return digest(self.config)

    def validate(self, test_root=None):
        require(all(type(value) is str and 0 < len(value) <= 500 for value in (self.provider, self.exact_model, self.preprocessing_version, self.provenance)), 'INVALID_PROFILE', 'explicit bounded profile identity fields required')
        require(type(self.dim) is int and 1 <= self.dim <= 8192 and self.metric == 'cosine', 'INVALID_PROFILE', 'unsupported dimension/metric')
        require(self.provider not in ('unknown', 'legacy_unknown') and bool(self.exact_model), 'UNKNOWN_PROFILE', 'model provenance must be explicit')
        if self.provider == 'mock':
            require(test_root is not None, 'MOCK_FORBIDDEN', 'mock embedding only allowed in explicit isolated test profile')
            bounded(test_root)

    def vector(self, vector):
        require(type(vector) is list and len(vector) == self.dim and all(type(value) in (int, float) and math.isfinite(value) for value in vector),
                'INVALID_VECTOR', 'vector dimension or values invalid')
        require(sum(value * value for value in vector) > 0, 'INVALID_VECTOR', 'zero vector is invalid')
        return vector


def cosine(left, right):
    return sum(first * second for first, second in zip(left, right)) / math.sqrt(sum(value * value for value in left) * sum(value * value for value in right))


class APIEmbedding:
    def __init__(self, profile, transport, max_texts=100, max_chars=200000):
        profile.validate()
        self.profile = profile
        self.transport = transport
        self.max_texts = max_texts
        self.max_chars = max_chars

    def __call__(self, texts):
        require(len(texts) <= self.max_texts and sum(map(len, texts)) <= self.max_chars,
                'NEEDS_SCOPE_APPROVAL', 'embedding budget exceeded')
        response = self.transport({'model': self.profile.exact_model, 'input': texts, 'dimensions': self.profile.dim})
        require(response.get('model', self.profile.exact_model) == self.profile.exact_model, 'PROVIDER_RESPONSE', 'provider returned a different model identity')
        data = sorted(response['data'], key=lambda item: item['index'])
        require([item['index'] for item in data] == list(range(len(texts))), 'PROVIDER_RESPONSE', 'missing/duplicate embedding indices')
        return [self.profile.vector(item['embedding']) for item in data]


class JSONAPITransport:
    def __init__(self, endpoint, authorization=None, max_response_bytes=8 * 1024 * 1024, timeout=None):
        self.endpoint = endpoint
        self.authorization = authorization
        self.max_response_bytes = max_response_bytes
        self.timeout = timeout

    def __call__(self, payload):
        import json
        from .network import request, validate_url
        from .errors import CorpusError
        import os
        import socket
        require(os.environ.get('CORPUS_OFFLINE') != '1', 'NETWORK_DISABLED', 'offline profile forbids provider DNS and requests')
        try:
            url, host, address = validate_url(self.endpoint)
        except socket.gaierror as error:
            raise CorpusError('PROVIDER_NOT_SENT', 'provider DNS failed before HTTP request; safe to resume', retryable=True) from error
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self.authorization is not None:
            require(type(self.authorization) is str and '\r' not in self.authorization and '\n' not in self.authorization,
                    'INVALID_AUTH', 'invalid explicit authorization header')
            headers['Authorization'] = self.authorization
        body = json.dumps(payload, allow_nan=False).encode()
        options = {} if self.timeout is None else {'timeout': self.timeout}
        status, response_headers, response = request(url, host, address, self.max_response_bytes,
                                                     method='POST', body=body, headers=headers, **options)
        if status == 429:
            raise CorpusError('PROVIDER_RATE_LIMITED', 'provider rejected request due to rate limit', retryable=True)
        if status in (500, 502, 503, 504):
            raise CorpusError('PROVIDER_RETRYABLE', 'provider temporarily unavailable', retryable=True)
        require(status == 200, 'PROVIDER_FAILED', 'embedding API request failed; redirects not followed')
        return json.loads(response)
