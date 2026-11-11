import importlib.util
import shutil
import sqlite3

from .paths import data_root


def probe(root):
    root = data_root(root)
    with sqlite3.connect(root / 'probe.sqlite') as connection:
        connection.execute('CREATE VIRTUAL TABLE IF NOT EXISTS probe USING fts5(text)')
        connection.execute('DELETE FROM probe')
        connection.execute('INSERT INTO probe VALUES (?)', ('中文研究资料',))
        exact = connection.execute("SELECT count(*) FROM probe WHERE probe MATCH '研究'").fetchone()[0]
        fallback = connection.execute("SELECT count(*) FROM probe WHERE instr(text, '研究') > 0").fetchone()[0]
    return {'fts5': True, 'chinese_unicode61_partial_match': bool(exact),
            'chinese_substring_fallback': bool(fallback),
            'sdk': 'unavailable_worker_parent_verification_required',
            'approval': 'disabled_no_trusted_host_adapter',
            'purge': 'disabled_no_trusted_host_adapter',
            'pdf': 'isolated_pypdf_subprocess' if importlib.util.find_spec('pypdf') else 'unavailable_optional_pypdf',
            'sqlite_vec_installed': importlib.util.find_spec('sqlite_vec') is not None,
            'vector_backend': 'stdlib_exact_bounded',
            'real_embedding': 'not_called_no_provider_authorized'}
