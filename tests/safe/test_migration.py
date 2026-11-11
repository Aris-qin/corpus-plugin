import hashlib
import json
import sqlite3

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


def test_readonly_wal_migration_preserves_every_field_relation_and_old_id(root):
    from corpus_core.migration import migrate
    root.mkdir(parents=True)
    legacy = root / 'SYNTHETIC-legacy.sqlite'
    source = sqlite3.connect(legacy)
    source.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE documents(pmid TEXT PRIMARY KEY,title TEXT,source TEXT,raw_path TEXT,extra BLOB,created_at TEXT);
        CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,pmid TEXT,text TEXT,char_start INTEGER,embedding BLOB);
        CREATE TABLE document_groups(pmid TEXT,project TEXT,extra TEXT);
        INSERT INTO documents VALUES ('old1','SYNTHETIC original','file_parse','raw/old.txt',X'00FF','unknown local time');
        INSERT INTO chunks VALUES ('oldchunk','old1','SYNTHETIC evidence',7,X'00000000');
        INSERT INTO document_groups VALUES ('old1','project-a','raw relation');
    ''')
    source.commit()
    corpus = Corpus(root / 'destination')
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    evidence = migrate(corpus, legacy)
    assert evidence['verified'] and evidence['counts'] == {'documents': 1, 'chunks': 1, 'document_groups': 1}
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
    assert migrate(corpus, legacy)['source_hash'] == evidence['source_hash']
    assert corpus.db.execute('SELECT count(*) FROM legacy_map').fetchone()[0] == 3
    result = corpus.resolve_legacy('oldchunk')
    assert result['matches'][0]['resolved']['chunk']['text'] == 'SYNTHETIC evidence'
    raw = json.loads(corpus.db.execute("SELECT raw_payload FROM legacy_map WHERE table_name='documents'").fetchone()[0])
    assert raw['extra'] == {'$bytes': 'AP8='}
    assert corpus.db.execute('SELECT count(*) FROM research_records').fetchone()[0] == 0
    assert corpus.db.execute('SELECT count(*) FROM migration_issues').fetchone()[0] >= 2
    assert corpus.search('evidence')['hits']
    source.close()


def test_unknown_schema_and_outside_root_refused(root):
    from corpus_core.migration import migrate
    corpus = Corpus(root)
    unknown = root / 'SYNTHETIC-unknown.sqlite'
    with sqlite3.connect(unknown) as source:
        source.execute('CREATE TABLE unrelated(id TEXT)')
    with pytest.raises(CorpusError, match='schema'):
        migrate(corpus, unknown)
    with pytest.raises(CorpusError):
        migrate(corpus, '/workspace/forbidden.sqlite')
