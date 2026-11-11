import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus
from test_retrieval import seed


def settings(root, operations=('submit',), max_calls=10):
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'SYNTHETIC-settings.json'
    payload = {'data_root': str(root / 'data'), 'embedding': {
        'profile': {'provider': 'configured-api', 'exact_model': 'SYNTHETIC-fixture-model', 'dim': 2},
        'endpoint': 'https://api.example.test/embeddings', 'key_env': 'SYNTHETIC_UNUSED_KEY',
        'policy': {'operation_ids': list(operations), 'max_calls': max_calls, 'max_chars': 10000,
                   'batch_size': 1, 'max_cost_usd': 1.0, 'max_cost_per_call_usd': 0.01}}}
    path.write_text(json.dumps(payload))
    return path


def test_configured_embedding_submit_runner_query_and_checkpoints(root):
    calls = []
    def transport(payload):
        calls.append(payload)
        assert not corpus.db.in_transaction
        return {'data': [{'index': index, 'embedding': [1.0, 0.0]} for index, text in enumerate(payload['input'])]}
    corpus = Corpus.from_settings(settings(root), test_mode=True, embedding_transport=transport)
    item = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC first\nsecond'}], 'operation_id': 'submit'})['items'][0]
    assert corpus.run_one(fail_before_activate=True)['state'] == 'failed'
    assert len(calls) == 2
    corpus.jobs_control('retry', item['job_id'], 'retry')
    result = corpus.run_one()
    assert result['state'] == 'succeeded' and len(calls) == 2
    assert json.loads(result['checkpoint'])['vector_status'] == 'complete'
    result = corpus.search('unmatched semantic query')
    assert result['vector_candidates'] == 2 and result['hits']
    assert len(calls) == 3


def test_ambiguous_embedding_failure_never_blind_retried(root):
    calls = []
    def transport(payload):
        calls.append(payload)
        raise TimeoutError('SYNTHETIC possibly billed')
    corpus = Corpus.from_settings(settings(root), test_mode=True, embedding_transport=transport)
    item = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC evidence'}], 'operation_id': 'submit'})['items'][0]
    job = corpus.run_one()
    assert len(calls) == 1 and job['state'] == 'partial'
    with pytest.raises(CorpusError) as blocked:
        corpus.jobs_control('retry', item['job_id'], 'retry')
    assert blocked.value.code == 'AMBIGUOUS_REVIEW_REQUIRED'
    assert json.loads(job['checkpoint'])['vector_status'] == 'incomplete_ambiguous'
    result = corpus.search('evidence')
    assert result['hits'] and 'embedding_incomplete_ambiguous' in result['warnings']
    assert len(calls) == 1


def test_embedding_budget_and_scope_are_enforced(root):
    calls = []
    def transport(payload):
        calls.append(payload)
        return {'data': [{'index': index, 'embedding': [1.0, 0.0]} for index, text in enumerate(payload['input'])]}
    corpus = Corpus.from_settings(settings(root, max_calls=1), test_mode=True, embedding_transport=transport)
    seed(corpus, 'SYNTHETIC evidence', 'submit')
    result = corpus.search('evidence')
    assert result['hits'] and len(calls) == 1
    assert 'embedding_budget_exhausted' in result['warnings']
    corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC unauthorized', 'content': 'SYNTHETIC unauthorized'}], 'operation_id': 'not-approved'})
    job = corpus.run_one()
    assert job['state'] == 'partial'
    assert json.loads(job['checkpoint'])['vector_status'] == 'scope_not_approved'
    assert len(calls) == 1


def test_pdf_real_pages_hashes_and_unextracted_warnings(root):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    root.mkdir(parents=True)
    source = root / 'SYNTHETIC.pdf'
    writer = PdfWriter()
    for text in ('SYNTHETIC first_page', 'SYNTHETIC second_page'):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 10 250 Td (' + text + ') Tj ET').encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(source)
    corpus = Corpus(root)
    item = corpus.ingest({'inputs': [{'source': 'file', 'path': str(source), 'title': 'SYNTHETIC PDF', 'media_type': 'application/pdf'}], 'operation_id': 'pdf'})['items'][0]
    job = corpus.run_one()
    assert job['state'] == 'succeeded', job
    version = corpus.read('corpus://version/' + item['version_id'])
    chunks = corpus.db.execute('SELECT * FROM chunks ORDER BY ordinal').fetchall()
    assert {json.loads(chunk['locator'])['page'] for chunk in chunks} == {1, 2}
    for chunk in chunks:
        locator = json.loads(chunk['locator'])
        assert locator['original_hash'] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert locator['canonical_hash'] == version['generation']['canonical_hash']
        assert version['generation']['canonical'][locator['char_start']:locator['char_end']] == chunk['text']
    assert 'figures_formulas_ocr_not_extracted' in json.loads(version['generation']['warnings'])


def test_actual_vec0_ddl_preserved_and_unknown_module_refused(root):
    import sqlite_vec
    from corpus_core.migration import migrate
    root.mkdir(parents=True)
    source = root / 'SYNTHETIC-vec0.sqlite'
    schema = json.loads((Path(__file__).parents[2] / 'LEGACY_SCHEMA.json').read_text())
    with sqlite3.connect(source) as connection:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        for name, ddl in schema['schema']:
            if name != 'sqlite_sequence' and not (name.startswith('chunk_vectors_')):
                connection.execute(ddl)
        connection.execute("INSERT INTO documents(pmid,title,source) VALUES ('old','SYNTHETIC','file_parse')")
        connection.execute("INSERT INTO chunks(chunk_id,pmid,text) VALUES ('chunk','old','SYNTHETIC evidence')")
        connection.execute("INSERT INTO projects(slug,title,status) VALUES ('stable','SYNTHETIC label','active')")
        connection.execute("INSERT INTO document_groups(pmid,project_slug) VALUES ('old','stable')")
        connection.execute('INSERT INTO chunk_vectors(chunk_id,embedding) VALUES (?,?)', ('chunk', sqlite_vec.serialize_float32([1.0] * 1024)))
    corpus = Corpus(root / 'data')
    evidence = migrate(corpus, source)
    assert evidence['verified'] and evidence['counts']['chunk_vectors'] == 1
    raw_tables = {row['table_name'] for row in corpus.db.execute('SELECT table_name FROM legacy_map')}
    assert {'chunk_vectors', 'chunk_vectors_info', 'chunk_vectors_chunks', 'chunk_vectors_vector_chunks00'} <= raw_tables
    context = corpus.db.execute("SELECT * FROM contexts WHERE id='stable'").fetchone()
    assert 'SYNTHETIC label' in context['aliases'] and 'active' in context['aliases']
    assert corpus.db.execute('SELECT profile FROM legacy_vectors').fetchone()['profile'] == 'legacy_unknown'
    assert not corpus.db.execute('SELECT 1 FROM chunk_embeddings').fetchone()


def test_safe_url_and_deterministic_doi_acquisition(root):
    seen = []
    def fetch(url):
        seen.append(url)
        return {'content': b'SYNTHETIC acquired evidence', 'url': 'https://example.test/article',
                'media_type': 'text/plain', 'redirects': 1, 'dns_pinned': True}
    corpus = Corpus(root, test_mode=True, acquisition=fetch)
    item = corpus.ingest({'inputs': [{'source': 'identifier', 'identifier': '10.1234/ABC', 'title': 'SYNTHETIC'}], 'operation_id': 'doi'})['items'][0]
    assert seen == ['https://doi.org/10.1234/abc']
    corpus.run_one()
    version = corpus.read('corpus://version/' + item['version_id'])['version']
    assert json.loads(version['metadata'])['acquisition']['url'] == 'https://example.test/article'
    with pytest.raises(CorpusError):
        corpus.ingest({'inputs': [{'source': 'identifier', 'identifier': 'invalid', 'title': 'SYNTHETIC'}], 'operation_id': 'invalid'})
    assert len(seen) == 1


def test_filters_before_budget_and_chunk_expansion(root):
    corpus = Corpus(root)
    seed(corpus, 'SYNTHETIC wrongscope', 'wrong')
    item = seed(corpus, 'SYNTHETIC before\nevidence\nafter', 'right')
    result = corpus.search('evidence', filters={'work_ids': [item['work_id']], 'version_kinds': ['snapshot']}, candidate_budget=3)
    assert not result['truncated'] and result['candidate_count'] == 1
    assert result['retrieval_diagnostics']['eligible_chunks_scanned'] == 3
    expanded = corpus.expand_chunk(result['hits'][0]['ref'], before=1, after=1)
    assert len(expanded['context']) == 3
    assert all(row['generation_id'] == expanded['chunk']['generation_id'] for row in expanded['context'])


def test_missing_authorized_root_fails_closed(root, monkeypatch):
    monkeypatch.delenv('CORPUS_AUTHORIZED_SANDBOX_ROOT', raising=False)
    with pytest.raises(CorpusError):
        Corpus(root)
    corpus = Corpus(root, sandbox_root=root.parent)
    corpus.close()


def test_job_cannot_spend_expanded_changed_policy(root):
    calls = []
    path = settings(root)
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=lambda payload: calls.append(payload))
    corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC'}], 'operation_id': 'submit'})
    corpus.close()
    changed = json.loads(path.read_text())
    changed['embedding']['policy']['max_calls'] = 100
    path.write_text(json.dumps(changed))
    corpus = Corpus.from_settings(path, test_mode=True, embedding_transport=lambda payload: calls.append(payload))
    result = corpus.run_one()
    assert calls == []
    assert json.loads(result['checkpoint'])['vector_status'] == 'configuration_changed'


def test_unknown_future_legacy_columns_rejected(root):
    from corpus_core.migration import migrate
    root.mkdir(parents=True)
    source = root / 'SYNTHETIC-future.sqlite'
    with sqlite3.connect(source) as connection:
        connection.executescript('CREATE TABLE documents(pmid TEXT,title TEXT,future_semantic_revision TEXT);'
            'CREATE TABLE chunks(chunk_id TEXT,pmid TEXT,text TEXT); CREATE TABLE document_groups(pmid TEXT);')
    with pytest.raises(CorpusError, match='DDL'):
        migrate(Corpus(root / 'data'), source)


def test_login_acquisition_is_truthful_and_preserves_no_fake_paper(root):
    def fetch(url):
        return {'content': b'<html><input type="password"></html>', 'url': url, 'media_type': 'text/html', 'redirects': 0, 'dns_pinned': True}
    corpus = Corpus(root, test_mode=True, acquisition=fetch)
    with pytest.raises(CorpusError, match='login'):
        corpus.ingest({'inputs': [{'source': 'url', 'url': 'https://example.test/login', 'title': 'SYNTHETIC'}], 'operation_id': 'login'})
    assert corpus.db.execute('SELECT count(*) FROM source_versions').fetchone()[0] == 0


def test_unknown_virtual_module_refused_after_backup(root):
    from corpus_core.migration import migrate
    root.mkdir(parents=True)
    source = root / 'SYNTHETIC-unknown-virtual.sqlite'
    with sqlite3.connect(source) as connection:
        connection.executescript('CREATE TABLE documents(pmid TEXT,title TEXT); CREATE TABLE chunks(chunk_id TEXT,pmid TEXT,text TEXT);'
            'CREATE TABLE document_groups(pmid TEXT); CREATE VIRTUAL TABLE unknown_future USING fts5(text);')
    corpus = Corpus(root / 'data')
    with pytest.raises(CorpusError):
        migrate(corpus, source)
    assert list((corpus.root / 'objects').glob('sha256/*/*'))
    assert corpus.db.execute('SELECT count(*) FROM works').fetchone()[0] == 0


def test_real_configured_json_transport_path_with_offline_http_fixture(root, monkeypatch):
    import corpus_core.network as network
    requests = []
    def request(url, host, address, max_bytes, method, body, headers):
        assert not corpus.db.in_transaction
        assert method == 'POST' and headers['Authorization'] == 'Bearer SYNTHETIC-NOT-A-SERVICE-KEY'
        payload = json.loads(body)
        assert payload['dimensions'] == 2
        requests.append(payload)
        return 200, {}, json.dumps({'model': payload['model'], 'data': [
            {'index': index, 'embedding': [1.0, 0.0]} for index, text in enumerate(payload['input'])]}).encode()
    monkeypatch.delenv('CORPUS_OFFLINE')
    monkeypatch.setenv('SYNTHETIC_UNUSED_KEY', 'SYNTHETIC-NOT-A-SERVICE-KEY')
    monkeypatch.setattr(network, 'validate_url', lambda endpoint: (endpoint, 'api.example.test', '93.184.216.34'))
    monkeypatch.setattr(network, 'request', request)
    corpus = Corpus.from_settings(settings(root))
    assert not corpus.test_mode
    seed(corpus, 'SYNTHETIC evidence', 'submit')
    assert corpus.search('semantic fixture')['vector_candidates'] == 1
    assert len(requests) == 2


def test_production_path_refuses_injected_transports_and_mock(root):
    path = settings(root)
    with pytest.raises(CorpusError):
        Corpus.from_settings(path, embedding_transport=lambda payload: {})
    payload = json.loads(path.read_text())
    payload['embedding']['profile']['provider'] = 'mock'
    path.write_text(json.dumps(payload))
    with pytest.raises(CorpusError):
        Corpus.from_settings(path)
    with pytest.raises(CorpusError):
        Corpus(root / 'other', acquisition=lambda url: {})


def test_url_submission_uses_ssrf_redirect_adapter(root, monkeypatch):
    import corpus_core.network as network
    original = network.fetch_snapshot
    requests = []
    def transport(url, host, address, limit):
        requests.append(url)
        assert address == '93.184.216.34'
        if len(requests) == 1:
            return 302, {'location': '/article'}, b''
        return 200, {'content-type': 'text/plain; charset=utf-8'}, b'SYNTHETIC acquired'
    monkeypatch.delenv('CORPUS_OFFLINE')
    monkeypatch.setattr(network, 'fetch_snapshot', lambda url: original(url, resolver=lambda host, port: ['93.184.216.34'], transport=transport))
    corpus = Corpus(root)
    item = corpus.ingest({'inputs': [{'source': 'url', 'url': 'https://example.test/start', 'title': 'SYNTHETIC'}], 'operation_id': 'url'})['items'][0]
    assert requests == ['https://example.test/start', 'https://example.test/article']
    corpus.run_one()
    version = corpus.read('corpus://version/' + item['version_id'])
    assert json.loads(version['version']['metadata'])['acquisition']['redirects'] == 1
    assert version['coverage'] == 'unknown'


def test_related_reports_show_drafts_then_formal_without_evidence_substitution(root, simulated_approval_boundary):
    from test_research import draft
    from test_parent_corrections import approved
    corpus = Corpus(root)
    item = seed(corpus)
    revision = draft(corpus, 'corpus://version/' + item['version_id'])
    related = corpus.search('evidence')['related_reports']['items'][0]
    assert related['current'] is None and related['drafts'][0]['id'] == revision['revision_id']
    approved(corpus, corpus.propose_publish(revision['revision_id'], 'publish'))
    result = corpus.search('evidence')
    related = result['related_reports']['items'][0]
    assert related['current']['id'] == revision['revision_id'] and not related['drafts']
    assert result['hits'][0]['ref'].startswith('corpus://chunk/')


def test_generated_nested_schemas_are_valid_json_schema():
    import jsonschema
    from corpus_core.contracts import CONTRACTS, schema_for
    for name in CONTRACTS:
        jsonschema.Draft202012Validator.check_schema(schema_for(name)['parameters'])
