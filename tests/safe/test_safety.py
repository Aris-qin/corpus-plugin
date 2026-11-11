import json
import socket
import subprocess
import sys

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus
from test_retrieval import seed


def test_cli_requires_root_and_can_ingest_run_search(root):
    def call(*args):
        return subprocess.run([sys.executable, '-m', 'corpus_core.cli', *args], capture_output=True, text=True)
    assert call('probe').returncode != 0
    assert call('--root', str(root), 'probe').returncode == 0
    payload = {'action': 'submit', 'operation_id': 'cli', 'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC CLI evidence'}]}
    assert json.loads(call('--root', str(root), 'call', 'corpus_ingest', '--json', json.dumps(payload)).stdout)['ok']
    assert json.loads(call('--root', str(root), 'runner', '--max-jobs', '1').stdout)['processed'] == 1
    assert json.loads(call('--root', str(root), 'call', 'corpus_search', '--json', '{"mode":"evidence","query":"evidence"}').stdout)['result']['hits']


def test_ssrf_redirect_dns_and_response_limits(root):
    from corpus_core.network import fetch_snapshot
    def resolver(host, port):
        return ['93.184.216.34'] if host == 'example.org' else ['127.0.0.1']
    for url in ('file:///etc/passwd', 'https://localhost/', 'https://user:pass@example.org/', 'https://example.org:444/', 'http://example.org/'):
        with pytest.raises(CorpusError):
            fetch_snapshot(url, resolver=resolver, transport=lambda *args: None)
    with pytest.raises(CorpusError, match='public'):
        fetch_snapshot('https://example.org/', resolver=resolver,
                       transport=lambda *args: (302, {'location': 'https://localhost/'}, b''))
    with pytest.raises(CorpusError, match='size'):
        fetch_snapshot('https://example.org/', max_bytes=3, resolver=resolver,
                       transport=lambda *args: (200, {}, b'toolarge'))
    result = fetch_snapshot('https://example.org/', resolver=resolver,
                            transport=lambda *args: (200, {'content-type': 'text/plain'}, b'SYNTHETIC snapshot'))
    assert result['content'] == b'SYNTHETIC snapshot'


def test_version_upgrade_old_citation_and_identity(root):
    corpus = Corpus(root)
    old = seed(corpus)
    new = corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC formal', 'content': 'SYNTHETIC revised evidence',
                                     'work_id': old['work_id'], 'expected_revision': 1, 'version_kind': 'formal',
                                     'relationship_source': 'corpus://version/' + old['version_id']}], 'operation_id': 'formal'})['items'][0]
    assert new['work_id'] == old['work_id'] and new['version_id'] != old['version_id']
    assert corpus.read('corpus://version/' + old['version_id'])['version']['id'] == old['version_id']
    assert corpus.read('corpus://work/' + old['work_id'])['version']['id'] == new['version_id']
    from corpus_core.identity import normalize_identifier
    assert normalize_identifier('doi', 'https://doi.org/10.1000/TEST') == '10.1000/test'
    assert normalize_identifier('url', 'https://EXAMPLE.org/a?b=2&a=1') == 'https://example.org/a?b=2&a=1'
    with pytest.raises(CorpusError):
        normalize_identifier('pmid', 'wrong')


def test_parser_symlink_and_schema_constraints(root):
    corpus = Corpus(root)
    outside = root.parent / 'SYNTHETIC-outside.txt'
    outside.write_text('SYNTHETIC do not read')
    (root / 'escape').symlink_to(outside)
    with pytest.raises(CorpusError):
        corpus.ingest({'inputs': [{'source': 'file', 'path': str(root / 'escape'), 'title': 'SYNTHETIC'}], 'operation_id': 'escape'})
    first = seed(corpus)
    second = seed(corpus, 'SYNTHETIC second source', 'second')
    generation = corpus.db.execute('SELECT generation_id FROM active_generations WHERE version_id=?', (first['version_id'],)).fetchone()[0]
    with pytest.raises(Exception):
        corpus.db.execute('UPDATE active_generations SET generation_id=? WHERE version_id=?', (generation, second['version_id']))
    from corpus_core.parsing import parse
    html = root / 'SYNTHETIC.html'
    html.write_text('<h1>Heading</h1><p>visible 中文</p><script>not evidence</script>')
    text, _, chunks, warnings = parse(root, html, 'text/html')
    assert 'not evidence' not in text and 'visible' in text
    assert chunks[0]['locator']['page'] is None and warnings
