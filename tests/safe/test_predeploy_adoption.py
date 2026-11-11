import hashlib
import json
import sqlite3
import struct

import pytest

from corpus_core.db import digest, encoded
from corpus_core.errors import CorpusError
from corpus_core.migration import migrate, raw_value
from corpus_core.service import Corpus


def legacy(root, original_hash=None, verifier=None):
    root.mkdir(parents=True)
    source = root / 'SYNTHETIC-legacy.sqlite'
    original = root / 'original.txt'
    original.write_text('SYNTHETIC original evidence')
    with sqlite3.connect(source) as database:
        database.executescript('CREATE TABLE documents(pmid TEXT PRIMARY KEY,title TEXT,source TEXT,raw_path TEXT,extra TEXT);'
                              'CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,pmid TEXT,text TEXT,parent_id TEXT);'
                              'CREATE TABLE document_groups(pmid TEXT,project TEXT);')
        extra = {'original_sha256': hashlib.sha256(original.read_bytes()).hexdigest()} if original_hash else {}
        database.execute('INSERT INTO documents VALUES (?,?,?,?,?)', ('old', 'SYNTHETIC', 'file_parse', 'original.txt', json.dumps(extra)))
        database.execute("INSERT INTO chunks VALUES ('chunk','old','SYNTHETIC original evidence','missing-parent')")
        for ordinal in range(15):
            database.execute('INSERT INTO documents VALUES (?,?,?,?,?)', (str(ordinal), 'SYNTHETIC review', 'self_written' if ordinal < 7 else 'unknown', None, '{}'))
            database.execute('INSERT INTO chunks VALUES (?,?,?,NULL)', ('review-' + str(ordinal), str(ordinal), 'SYNTHETIC unresolved'))
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    corpus = Corpus(root / 'candidate') if verifier is None else Corpus(root / 'candidate', legacy_verifier=verifier)
    evidence = migrate(corpus, source)
    mapping = corpus.resolve_legacy('old')['matches'][0]
    manifest = {'source_hash': evidence['source_hash'], 'legacy_id': 'old', 'path': str(original),
                'expected_sha256': hashlib.sha256(original.read_bytes()).hexdigest(), 'expected_size': original.stat().st_size,
                'identity_evidence': {'kind': 'preserved_original_sha256'}}
    return corpus, source, before, original, manifest, mapping


def test_original_dryrun_adopt_replay_and_original_unchanged(root):
    corpus, source, before, original, manifest, mapping = legacy(root, original_hash=True)
    data = original.read_bytes()
    assert not corpus.original_adoption(manifest)['blockers']
    assert corpus.db.execute('SELECT count(*) FROM legacy_adoptions').fetchone()[0] == 0
    result = corpus.original_adoption(manifest, dry_run=False)
    assert result['state'] == 'managed_copy'
    assert original.read_bytes() == data
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    original.unlink()
    assert corpus.original_adoption(manifest, dry_run=False)['replayed']
    assert corpus.db.execute('SELECT count(*) FROM version_assets').fetchone()[0] == 1
    assert json.loads(corpus.db.execute('SELECT metadata FROM works WHERE id=?', (mapping['target_id'].split('/')[-1],)).fetchone()[0])['custody'] == 'managed_copy'


@pytest.mark.parametrize('fault', ['no_historical_hash', 'wrong_content', 'missing', 'escape'])
def test_original_identity_and_path_blockers_never_write(root, fault):
    corpus, source, before, original, manifest, mapping = legacy(root, original_hash=fault != 'no_historical_hash')
    if fault == 'wrong_content':
        duplicate = root / 'different' / 'original.txt'
        duplicate.parent.mkdir()
        duplicate.write_text('SYNTHETIC different same filename')
        manifest['path'] = str(duplicate)
    elif fault == 'missing':
        manifest['path'] = str(root / 'missing.txt')
    elif fault == 'escape':
        (root / 'escape').symlink_to('/tmp', target_is_directory=True)
        manifest['path'] = str(root / 'escape' / 'original.txt')
    changes = corpus.db.total_changes
    if fault != 'escape':
        assert corpus.original_adoption(manifest)['blockers']
    with pytest.raises(CorpusError):
        corpus.original_adoption(manifest, dry_run=False)
    assert corpus.db.total_changes == changes
    assert corpus.db.execute('SELECT count(*) FROM version_assets').fetchone()[0] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_original_failure_rolls_back_copy_and_business_state(root):
    corpus, source, before, original, manifest, mapping = legacy(root, original_hash=True)
    corpus.db.execute("CREATE TRIGGER synthetic_adoption_fault BEFORE INSERT ON legacy_adoptions BEGIN SELECT RAISE(ABORT,'SYNTHETIC failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        corpus.original_adoption(manifest, dry_run=False)
    assert corpus.db.execute('SELECT count(*) FROM version_assets').fetchone()[0] == 0
    assert corpus.db.execute('SELECT count(*) FROM blobs').fetchone()[0] == 0
    assert not (corpus.root / 'objects' / 'sha256' / manifest['expected_sha256'][:2] / manifest['expected_sha256']).exists()
    assert original.exists() and hashlib.sha256(source.read_bytes()).hexdigest() == before
    corpus.db.execute('DROP TRIGGER synthetic_adoption_fault')
    assert corpus.original_adoption(manifest, dry_run=False)['state'] == 'managed_copy'
    with pytest.raises(CorpusError) as conflict:
        corpus.original_adoption({**manifest, 'identity_evidence': {'kind': 'changed'}}, dry_run=False)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'


def vector_manifest(corpus, manifest):
    chunk = corpus.db.execute("SELECT target_id FROM legacy_map WHERE table_name='chunks' AND legacy_id='chunk'").fetchone()[0].split('/')[-1]
    raw = encoded(raw_value(struct.pack('<ff', 1.0, 0.0)))
    corpus.db.execute('INSERT INTO legacy_vectors VALUES (?,?,?,?,?)', (manifest['source_hash'], 'chunk', chunk, 'legacy_unknown', raw))
    for orphan in ('orphan-1', 'orphan-2'):
        corpus.db.execute('INSERT INTO legacy_vectors VALUES (?,?,?,?,?)', (manifest['source_hash'], orphan, None, 'legacy_unknown', raw))
        corpus.db.execute('INSERT INTO migration_issues(source_hash,legacy_id,reason) VALUES (?,?,?)', (manifest['source_hash'], orphan, 'orphan_vector_preserved'))
    return {'source_hash': manifest['source_hash'], 'profile': {'provider': 'synthetic-provider', 'exact_model': 'SYNTHETIC-known-model', 'dim': 2},
            'vectors': {'chunk': digest(raw)}, 'evidence': {'authority': 'SYNTHETIC-independent-fixture', 'receipt_hash': 'f' * 64}}


def test_vector_claim_requires_independent_evidence_not_model_dimension(root):
    corpus, source, before, original, manifest, mapping = legacy(root)
    claim = vector_manifest(corpus, manifest)
    assert corpus.claim_legacy_profile(claim, dry_run=True)['blockers']
    for evidence in ({'comment': 'same dimension; guessed model'}, {'authority': 'model-self-confirmed'}):
        with pytest.raises(CorpusError) as blocked:
            corpus.claim_legacy_profile({**claim, 'evidence': evidence})
        assert blocked.value.code == 'PROVENANCE_REQUIRED'
    with pytest.raises(CorpusError) as mock:
        corpus.claim_legacy_profile({**claim, 'profile': {**claim['profile'], 'provider': 'mock'}})
    assert mock.value.code == 'MOCK_FORBIDDEN'
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 0
    assert 'legacy_vectors_quarantined_unverified_provenance' in corpus.search('evidence')['warnings']


def test_verified_subset_claim_is_idempotent_and_preserves_review_items(root):
    bindings = []
    def synthetic_verifier(kind, binding):
        bindings.append((kind, binding))
        return kind == 'vector_provenance' and binding['manifest']['evidence']['authority'] == 'SYNTHETIC-independent-fixture'
    corpus, source, before, original, manifest, mapping = legacy(root, verifier=synthetic_verifier)
    claim = vector_manifest(corpus, manifest)
    assert not corpus.claim_legacy_profile(claim, dry_run=True)['blockers']
    result = corpus.claim_legacy_profile(claim)
    assert result['selected'] == 1 and not result['reembedding']
    changes = corpus.db.total_changes
    corpus.claim_legacy_profile(claim)
    assert corpus.db.total_changes == changes
    assert corpus.db.execute("SELECT count(*) FROM legacy_vectors WHERE profile='legacy_unknown'").fetchone()[0] == 2
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 1
    from corpus_core.embeddings import Profile
    search = corpus.search('SYNTHETIC', profile=Profile(**claim['profile']), query_vector=[1.0, 0.0])
    assert search['vector_candidates'] == 1
    inventory = corpus.legacy_inventory()
    assert len([issue for issue in inventory['issues'] if issue['reason'] == 'unresolved_self_written_or_unknown_quarantined_from_search']) == 15
    assert len([vector for vector in inventory['vectors'] if vector['chunk_id'] is None]) == 2
    assert inventory['unresolved_relationships']
    assert bindings[-1][1]['target_chunks'] and hashlib.sha256(source.read_bytes()).hexdigest() == before
    with pytest.raises(CorpusError):
        corpus.claim_legacy_profile({**claim, 'vectors': {'orphan-1': next(iter(claim['vectors'].values()))}})


def test_profile_claim_rolls_back_on_late_failure(root):
    corpus, source, before, original, manifest, mapping = legacy(root, verifier=lambda *args: True)
    claim = vector_manifest(corpus, manifest)
    corpus.db.execute("CREATE TRIGGER synthetic_claim_fault BEFORE UPDATE ON legacy_vectors BEGIN SELECT RAISE(ABORT,'SYNTHETIC failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        corpus.claim_legacy_profile(claim)
    assert corpus.db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0] == 0
    assert corpus.db.execute('SELECT count(*) FROM embedding_profiles').fetchone()[0] == 0
    assert corpus.db.execute("SELECT count(*) FROM legacy_vectors WHERE profile='legacy_unknown'").fetchone()[0] == 3


def test_legacy_cannot_bypass_claim_by_bulk_embedding_api(root):
    from corpus_core.embeddings import Profile
    corpus, source, before, original, manifest, mapping = legacy(root)
    with pytest.raises(CorpusError) as blocked:
        corpus.index_vectors(Profile('api', 'guessed', 2), lambda texts: pytest.fail('unapproved legacy reembedding'))
    assert blocked.value.code == 'LEGACY_REEMBEDDING_FORBIDDEN'
