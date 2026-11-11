"""Permanent deletion tests use only explicit synthetic offline fixtures."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from corpus_core.errors import CorpusError
from corpus_core.service import Corpus


pytestmark = pytest.mark.usefixtures("simulated_approval_boundary")
from test_retrieval import seed


def approved(corpus, item, operation='purge'):
    proposal = corpus.propose_maintenance({'maintenance': 'purge', 'target': item['work_id'],
        'expected_revision': 2, 'reason': 'SYNTHETIC explicit permanent deletion', 'operation_id': operation + '-propose'})
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    assert approval['identity_verified'] is False
    return proposal['proposal_id'], approval['approval_ref'], operation


def recycled(corpus):
    item = seed(corpus)
    corpus.lifecycle(item['work_id'], 'recycled', 1, 'recycle')
    return item


def test_approved_purge_removes_bytes_and_searchable_history(root):
    corpus = Corpus(root)
    item = recycled(corpus)
    ref = 'corpus://version/' + item['version_id']
    paths = [root / row['path'] for row in corpus.db.execute('SELECT path FROM blobs')]
    request = approved(corpus, item)
    result = corpus.apply_proposal(*request)
    assert result['state'] == 'purged'
    assert not any(path.exists() for path in paths)
    assert corpus.apply_proposal(*request) == result
    assert not corpus.search('evidence')['hits']
    assert corpus.read(ref)['state'] == 'purged'
    assert corpus.db.execute('SELECT count(*) FROM chunk_fts').fetchone()[0] == 0
    assert not corpus.db.execute('PRAGMA foreign_key_check').fetchall()
    for table, column in [('chunks', 'text'), ('generations', 'canonical')]:
        assert all(not row[0] for row in corpus.db.execute(f'SELECT {column} FROM {table}'))


def test_active_purge_is_blocked(root):
    corpus = Corpus(root)
    item = seed(corpus)
    with pytest.raises(CorpusError) as failure:
        corpus.propose_maintenance({'maintenance': 'purge', 'target': item['work_id'],
            'expected_revision': 1, 'reason': 'SYNTHETIC', 'operation_id': 'active-purge'})
    assert failure.value.code == 'PURGE_BLOCKED'


@pytest.mark.parametrize('dependency', ['citation', 'research_body', 'review', 'external', 'alias', 'legacy', 'job', 'shared'])
def test_dependencies_block_without_mutation(root, dependency):
    corpus = Corpus(root)
    item = recycled(corpus)
    version = item['version_id']
    if dependency in ('citation', 'research_body'):
        corpus.db.execute("INSERT INTO research_records(id,kind) VALUES ('report','topic_report')")
        body = 'SYNTHETIC corpus://version/' + version if dependency == 'research_body' else 'SYNTHETIC report'
        corpus.db.execute("INSERT INTO research_revisions VALUES ('revision','report',NULL,?,'hash','[]','{}','','','now')", (body,))
        if dependency == 'citation':
            corpus.db.execute("INSERT INTO citations VALUES ('cite','revision',?,NULL,NULL,'{}')", (version,))
    elif dependency == 'review':
        corpus.db.execute("INSERT INTO review_events VALUES ('review','other','[]',?,'now','','[]')", ('corpus://version/' + version,))
    elif dependency == 'external':
        corpus.external_link('corpus://version/' + version, 'wiki:synthetic', 'SYNTHETIC', 'link')
    elif dependency == 'alias':
        corpus.db.execute("INSERT INTO works VALUES ('alias','other','SYNTHETIC',?,1,'recycled',NULL,'now')", (json.dumps({'merged_into': item['work_id']}),))
    elif dependency == 'legacy':
        corpus.db.execute("INSERT INTO legacy_map VALUES ('backup','documents','old',?,?)", (json.dumps({'text': 'SYNTHETIC evidence'}), item['work_id']))
    elif dependency == 'job':
        corpus.db.execute("UPDATE jobs SET state='running' WHERE id=?", (item['job_id'],))
    elif dependency == 'shared':
        corpus.db.execute("INSERT INTO works VALUES ('shared','other','SYNTHETIC','{}',1,'active',NULL,'now')")
        corpus.db.execute("INSERT INTO source_versions VALUES ('shared-v','shared','snapshot','now','unknown','{}')")
        corpus.db.execute("INSERT INTO version_assets SELECT 'shared-v',hash,role,media_type FROM version_assets WHERE version_id=?", (version,))
    paths = {root / row['path']: (root / row['path']).read_bytes() for row in corpus.db.execute('SELECT path FROM blobs')}
    with pytest.raises(CorpusError) as failure:
        approved(corpus, item)
    assert failure.value.code == 'PURGE_BLOCKED'
    assert all(path.read_bytes() == content for path, content in paths.items())
    assert not corpus.db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('change', ['job', 'context', 'reference', 'revision'])
def test_freshness_at_apply(root, change):
    corpus = Corpus(root)
    item = recycled(corpus)
    request = approved(corpus, item)
    if change == 'job':
        corpus.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (item['job_id'],))
    elif change == 'context':
        corpus.link(item['work_id'], 'SYNTHETIC-new', 'new-context')
    elif change == 'reference':
        corpus.external_link('corpus://version/' + item['version_id'], 'wiki:new', 'SYNTHETIC', 'link')
    else:
        corpus.db.execute('UPDATE works SET revision=revision+1 WHERE id=?', (item['work_id'],))
    with pytest.raises(CorpusError):
        corpus.apply_proposal(*request)
    assert corpus.db.execute('SELECT count(*) FROM blobs').fetchone()[0] == 1


@pytest.mark.parametrize('phase', ['before_intent_commit', 'after_intent_commit', 'after_stage', 'after_unlink', 'before_complete_commit'])
def test_interruption_recovery(root, monkeypatch, phase):
    corpus = Corpus(root)
    item = recycled(corpus)
    request = approved(corpus, item)
    def crash(checkpoint):
        if checkpoint == phase:
            raise RuntimeError('SYNTHETIC crash')
    monkeypatch.setattr(corpus, '_purge_checkpoint', crash)
    with pytest.raises(RuntimeError, match='SYNTHETIC crash'):
        corpus.apply_proposal(*request)
    corpus.close()
    recovered = Corpus(root)
    if phase == 'before_intent_commit':
        assert recovered.db.execute('SELECT count(*) FROM blobs').fetchone()[0] == 1
    result = recovered.apply_proposal(*request)
    assert result['state'] == 'purged'
    assert recovered.apply_proposal(*request) == result
    assert not recovered.db.execute('PRAGMA foreign_key_check').fetchall()
    assert recovered.db.execute("SELECT count(*) FROM audit WHERE event='purge_complete'").fetchone()[0] == 1


@pytest.mark.parametrize('unsafe', ['traversal', 'absolute', 'symlink', 'hardlink', 'corrupt'])
def test_path_containment(root, unsafe):
    corpus = Corpus(root)
    item = recycled(corpus)
    blob = corpus.db.execute('SELECT * FROM blobs').fetchone()
    path = root / blob['path']
    outside = root.parent / 'SYNTHETIC-unowned'
    outside.write_bytes(path.read_bytes())
    if unsafe in ('absolute', 'traversal'):
        corpus.db.execute('DROP TRIGGER immutable_blobs_UPDATE')
        corpus.db.execute('UPDATE blobs SET path=?', (str(outside) if unsafe == 'absolute' else '../SYNTHETIC-unowned',))
    elif unsafe == 'symlink':
        path.unlink()
        path.symlink_to(outside)
    elif unsafe == 'hardlink':
        import os
        os.link(path, root / 'SYNTHETIC-hardlink')
    else:
        path.write_bytes(b'SYNTHETIC corrupt')
    with pytest.raises(CorpusError):
        approved(corpus, item)
    assert outside.read_bytes() == b'SYNTHETIC \xe4\xb8\xad\xe6\x96\x87\xe7\xa0\x94\xe7\xa9\xb6 evidence'


@pytest.mark.parametrize('same_operation', [True, False])
def test_concurrent_apply(root, same_operation):
    corpus = Corpus(root)
    item = recycled(corpus)
    request = approved(corpus, item)
    corpus.close()
    def apply(operation):
        candidate = Corpus(root)
        try:
            return candidate.apply_proposal(request[0], request[1], operation)['state']
        except CorpusError as error:
            return error.code
        finally:
            candidate.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, ['purge', 'purge' if same_operation else 'other']))
    assert 'purged' in results
    assert set(results) <= ({'purged'} if same_operation else {'purged', 'APPROVAL_INVALID'})


def test_pending_cleanup_is_truthful_and_blocks_reattachment(root, monkeypatch):
    import corpus_core.purge as purge
    corpus = Corpus(root)
    item = recycled(corpus)
    request = approved(corpus, item)
    original = purge.os.unlink
    def fail(*args, **kwargs):
        raise OSError('SYNTHETIC disk failure')
    monkeypatch.setattr(purge.os, 'unlink', fail)
    result = corpus.apply_proposal(*request)
    assert result['state'] == 'cleanup_pending' and result['cleanup_error'] == 'OSError'
    assert corpus.read('corpus://version/' + item['version_id'])['state'] == 'cleanup_pending'
    assert not corpus.search('evidence')['hits']
    with pytest.raises(CorpusError, match='pending deletion'):
        corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC', 'content': 'SYNTHETIC 中文研究 evidence'}], 'operation_id': 'reattach'})
    monkeypatch.setattr(purge.os, 'unlink', original)
    assert corpus.apply_proposal(*request)['state'] == 'purged'


@pytest.mark.parametrize('reference_kind', ['work', 'version', 'chunk'])
def test_tombstones_cannot_become_evidence_or_restored(root, reference_kind):
    corpus = Corpus(root)
    item = recycled(corpus)
    chunk = corpus.db.execute('SELECT id FROM chunks').fetchone()[0]
    ref = 'corpus://' + reference_kind + '/' + {'work': item['work_id'], 'version': item['version_id'], 'chunk': chunk}[reference_kind]
    corpus.apply_proposal(*approved(corpus, item))
    assert corpus.read(ref)['content_available'] is False
    with pytest.raises(CorpusError):
        corpus.evidence_target(ref)
    with pytest.raises(CorpusError):
        corpus.lifecycle(item['work_id'], 'active', 3, 'restore')


def test_racing_replacement_after_stage_remains_pending(root, monkeypatch):
    corpus = Corpus(root)
    item = recycled(corpus)
    path = root / corpus.db.execute('SELECT path FROM blobs').fetchone()[0]
    def replace(phase):
        if phase == 'after_stage':
            path.write_bytes(b'SYNTHETIC unowned replacement')
    monkeypatch.setattr(corpus, '_purge_checkpoint', replace)
    result = corpus.apply_proposal(*approved(corpus, item))
    assert result['state'] == 'cleanup_pending'
    assert path.read_bytes() == b'SYNTHETIC unowned replacement'


@pytest.mark.parametrize('dependency', ['chunk_citation', 'transitive_report', 'version_provenance', 'identifier_provenance'])
def test_additional_evidence_links_block(root, dependency):
    corpus = Corpus(root)
    item = recycled(corpus)
    ref = 'corpus://version/' + item['version_id']
    if dependency in ('chunk_citation', 'transitive_report'):
        corpus.db.execute("INSERT INTO research_records(id,kind) VALUES ('first','topic_report')")
        corpus.db.execute("INSERT INTO research_revisions VALUES ('first-r','first',NULL,'SYNTHETIC','hash',?,'{}','','','now')", (json.dumps([ref]),))
        if dependency == 'chunk_citation':
            chunk = corpus.db.execute('SELECT id FROM chunks').fetchone()[0]
            corpus.db.execute("INSERT INTO citations VALUES ('cite','first-r',NULL,NULL,?,'{}')", (chunk,))
        else:
            corpus.db.execute("INSERT INTO research_records(id,kind) VALUES ('second','topic_report')")
            corpus.db.execute("INSERT INTO research_revisions VALUES ('second-r','second',NULL,'SYNTHETIC','hash','[\"corpus://reference/first/revision/first-r\"]','{}','','','now')")
    else:
        corpus.db.execute("INSERT INTO works VALUES ('other','other','SYNTHETIC','{}',1,'active',NULL,'now')")
        if dependency == 'version_provenance':
            corpus.db.execute("INSERT INTO source_versions VALUES ('other-v','other','snapshot','now','unknown',?)", (json.dumps({'relationship_source': ref}),))
        else:
            corpus.db.execute("INSERT INTO external_identifiers VALUES ('other','doi','synthetic','synthetic',?)", (ref,))
    with pytest.raises(CorpusError) as failure:
        approved(corpus, item)
    assert failure.value.code == 'PURGE_BLOCKED'


def test_other_sources_survive_and_immutability_returns(root):
    import sqlite3
    corpus = Corpus(root)
    first = seed(corpus, operation='first')
    second = seed(corpus, 'SYNTHETIC remaining evidence', operation='second')
    corpus.lifecycle(first['work_id'], 'recycled', 1, 'recycle')
    corpus.apply_proposal(*approved(corpus, first))
    assert corpus.read('corpus://version/' + second['version_id'])['generation']['canonical']
    assert corpus.search('remaining')['hits']
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        corpus.db.execute("UPDATE chunks SET text='SYNTHETIC overwrite'")


def test_idempotency_rejects_other_payload_and_fake_approval(root):
    corpus = Corpus(root)
    item = recycled(corpus)
    request = approved(corpus, item)
    with pytest.raises(CorpusError):
        corpus.apply_proposal(request[0], 'confirm=true', 'fake')
    corpus.apply_proposal(*request)
    with pytest.raises(CorpusError) as failure:
        corpus.apply_proposal(request[0], 'different', request[2])
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'


def test_ambiguous_job_ownership_blocks(root):
    corpus = Corpus(root)
    item = recycled(corpus)
    corpus.db.execute("INSERT INTO jobs(id,kind,payload,state,budget,created_at) VALUES ('ambiguous','index',?,'succeeded','{}','now')", (json.dumps({'version_id': 'other', 'note': item['work_id']}),))
    with pytest.raises(CorpusError) as failure:
        approved(corpus, item)
    assert failure.value.code == 'PURGE_BLOCKED'


def test_active_generation_snapshot_must_be_fresh(root):
    corpus = Corpus(root)
    item = recycled(corpus)
    request = approved(corpus, item)
    corpus.db.execute('DELETE FROM active_generations')
    with pytest.raises(CorpusError) as failure:
        corpus.apply_proposal(*request)
    assert failure.value.code == 'IMPACT_CHANGED'


def test_missing_original_blocks_before_intent(root):
    corpus = Corpus(root)
    item = recycled(corpus)
    (root / corpus.db.execute('SELECT path FROM blobs').fetchone()[0]).unlink()
    with pytest.raises(CorpusError):
        approved(corpus, item)
    assert corpus.db.execute('SELECT count(*) FROM purge_journal').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['metadata', 'link'])
def test_tombstone_is_not_a_new_content_channel(root, mutation):
    corpus = Corpus(root)
    item = recycled(corpus)
    other = seed(corpus, 'SYNTHETIC other evidence', 'other')
    corpus.apply_proposal(*approved(corpus, item))
    with pytest.raises(CorpusError):
        if mutation == 'metadata':
            corpus.metadata(item['work_id'], 3, {'title': 'SYNTHETIC replacement'}, 'corpus://version/' + other['version_id'], 'new-metadata')
        else:
            corpus.link(item['work_id'], 'SYNTHETIC-new-context', 'new-link')


def test_multiple_versions_and_retired_generations_are_redacted(root):
    corpus = Corpus(root)
    item = seed(corpus)
    corpus.ingest({'inputs': [{'source': 'text', 'title': 'SYNTHETIC second version', 'content': 'SYNTHETIC second-version evidence',
        'work_id': item['work_id'], 'expected_revision': 1, 'version_kind': 'formal',
        'relationship_source': 'corpus://version/' + item['version_id']}], 'operation_id': 'second-version'})
    corpus.run_one()
    corpus.db.execute("UPDATE jobs SET state='queued',payload=json_set(payload,'$.pipeline','SYNTHETIC-new-pipeline') WHERE id=?", (item['job_id'],))
    corpus.run_one()
    corpus.lifecycle(item['work_id'], 'recycled', 2, 'recycle')
    proposal = corpus.propose_maintenance({'maintenance': 'purge', 'target': item['work_id'], 'expected_revision': 3,
        'reason': 'SYNTHETIC permanent deletion of all versions', 'operation_id': 'all-versions'})
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    result = corpus.apply_proposal(proposal['proposal_id'], approval['approval_ref'], 'apply-all-versions')
    assert result['state'] == 'purged' and result['blobs_deleted'] == 2
    assert corpus.db.execute('SELECT count(*) FROM generations').fetchone()[0] == 3
    assert all(row[0] == '' for row in corpus.db.execute('SELECT canonical FROM generations'))
    assert not list((root / 'objects' / 'sha256').glob('*/*'))


def test_symlink_swap_at_staging_never_unlinks_external_bytes(root, monkeypatch):
    import corpus_core.purge as purge
    corpus = Corpus(root)
    item = recycled(corpus)
    path = root / corpus.db.execute('SELECT path FROM blobs').fetchone()[0]
    outside = root.parent / 'SYNTHETIC-not-owned'
    outside.write_bytes(b'SYNTHETIC unowned bytes')
    original = purge.os.rename
    def swap(source, destination, **kwargs):
        path.unlink()
        path.symlink_to(outside)
        return original(source, destination, **kwargs)
    monkeypatch.setattr(purge.os, 'rename', swap)
    result = corpus.apply_proposal(*approved(corpus, item))
    assert result['state'] == 'cleanup_pending'
    assert outside.read_bytes() == b'SYNTHETIC unowned bytes'


def test_purge_dispatch_preserves_six_tool_contract(root):
    from corpus_core.contracts import CONTRACTS
    from corpus_core.tools import dispatch
    corpus = Corpus(root)
    item = recycled(corpus)
    proposal = dispatch(corpus, 'corpus_manage', {'action': 'propose', 'maintenance': 'purge', 'target': item['work_id'],
        'expected_revision': 2, 'reason': 'SYNTHETIC explicit permanent deletion', 'operation_id': 'dispatch-propose'})
    approval = corpus.operator_approve(proposal['proposal_id'], proposal['hash'], proposal['binding'])
    assert dispatch(corpus, 'corpus_manage', {'action': 'apply', 'proposal_id': proposal['proposal_id'],
        'approval_ref': approval['approval_ref'], 'operation_id': 'dispatch-apply'})['state'] == 'purged'
    assert len(CONTRACTS) == 6


def test_missing_asset_inventory_cannot_claim_original_purged(root):
    corpus = Corpus(root)
    item = recycled(corpus)
    path = root / corpus.db.execute('SELECT path FROM blobs').fetchone()[0]
    corpus.db.execute('DROP TRIGGER immutable_version_assets_DELETE')
    corpus.db.execute('DELETE FROM version_assets')
    with pytest.raises(CorpusError) as failure:
        approved(corpus, item)
    assert failure.value.code == 'PURGE_BLOCKED'
    assert path.exists()
