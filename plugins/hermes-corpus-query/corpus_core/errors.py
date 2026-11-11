class CorpusError(Exception):
    def __init__(self, code, message, retryable=False, details=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def require(condition, code, message):
    if not condition:
        raise CorpusError(code, message)
