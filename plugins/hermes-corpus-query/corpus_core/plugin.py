import json
import os
import sqlite3

from .contracts import CONTRACTS, schema_for
from .db import identity
from .errors import CorpusError
from .service import Corpus
from .tools import dispatch


def handler_for(name):
    def handler(args, **kwargs):
        request_id = identity('request')
        corpus = None
        try:
            settings = os.environ.get('CORPUS_SETTINGS_FILE')
            corpus = Corpus.from_settings(settings) if settings else Corpus(os.environ.get('CORPUS_SANDBOX_ROOT'))
            result = dispatch(corpus, name, args)
            envelope = {'ok': True, 'request_id': request_id, 'result': result,
                        'warnings': result.get('warnings', []), 'error': None}
        except CorpusError as error:
            envelope = {'ok': False, 'request_id': request_id, 'result': None, 'warnings': [],
                        'error': {'code': error.code, 'message': str(error), 'retryable': error.retryable, 'details': error.details}}
        except sqlite3.OperationalError as error:
            busy = getattr(error, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            envelope = {'ok': False, 'request_id': request_id, 'result': None, 'warnings': [],
                        'error': {'code': 'DB_BUSY' if busy else 'DB_ERROR', 'message': 'candidate database operation failed', 'retryable': busy, 'details': {}}}
        except Exception:
            envelope = {'ok': False, 'request_id': request_id, 'result': None, 'warnings': [],
                        'error': {'code': 'INTERNAL_ERROR', 'message': 'candidate operation failed; inspect isolated diagnostics', 'retryable': False, 'details': {}}}
        finally:
            if corpus is not None:
                corpus.close()
        return json.dumps(envelope, ensure_ascii=False, allow_nan=False)
    return handler


def register(ctx):
    for name in CONTRACTS:
        schema = schema_for(name)
        ctx.register_tool(name=name, toolset='corpus', schema=schema, handler=handler_for(name),
                          description=schema['description'], requires_env=['CORPUS_AUTHORIZED_SANDBOX_ROOT'])
