import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager

from .db import audit, digest, encoded, now, transaction
from .errors import CorpusError, require


@contextmanager
def directory(path, boundary):
    descriptor = os.open(boundary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.relative_to(boundary).parts:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def inspect_file(parent, name, blob):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        status = os.fstat(descriptor)
        require(stat.S_ISREG(status.st_mode) and status.st_nlink == 1,
                'PURGE_UNSAFE_PATH', 'purge requires an exclusively owned regular file')
        content_hash = hashlib.sha256()
        while data := os.read(descriptor, 1024 * 1024):
            content_hash.update(data)
        require(status.st_size == blob['size'] and content_hash.hexdigest() == blob['hash'],
                'PURGE_UNSAFE_PATH', 'purge object hash or size changed')
        return {'device': status.st_dev, 'inode': status.st_ino}
    finally:
        os.close(descriptor)


class Purge:
    def _purge_checkpoint(self, phase):
        pass

    def purge_inventory(self, target):
        work = self.db.execute('SELECT * FROM works WHERE id=?', (target,)).fetchone()
        require(work is not None and work['lifecycle'] == 'recycled', 'PURGE_BLOCKED', 'only a recycled work can be permanently deleted')
        versions = [dict(row) for row in self.db.execute('SELECT * FROM source_versions WHERE work_id=? ORDER BY id', (target,))]
        version_ids = [row['id'] for row in versions]
        generations = [dict(row) for row in self.db.execute('SELECT * FROM generations WHERE version_id IN (SELECT value FROM json_each(?)) ORDER BY id', (encoded(version_ids),))]
        generation_ids = [row['id'] for row in generations]
        chunks = [dict(row) for row in self.db.execute('SELECT * FROM chunks WHERE generation_id IN (SELECT value FROM json_each(?)) ORDER BY id', (encoded(generation_ids),))]
        identifiers = [target, *version_ids, *generation_ids, *(row['id'] for row in chunks)]
        def mentions(row):
            return any(identifier in encoded(dict(row)) for identifier in identifiers)
        blockers = []
        for table in ('research_revisions', 'citations', 'external_links', 'review_events',
                      'legacy_map', 'legacy_relationships', 'journals'):
            if any(mentions(row) for row in self.db.execute('SELECT * FROM ' + table)):
                blockers.append(table)
        for row in self.db.execute('SELECT * FROM works'):
            if (row['id'] != target and mentions(row)) or (row['id'] == target and json.loads(row['metadata']).get('merged_into')):
                blockers.append('merged_identity')
        for row in self.db.execute('SELECT * FROM source_versions'):
            if row['work_id'] != target and mentions(row):
                blockers.append('version_provenance')
        for row in self.db.execute('SELECT * FROM external_identifiers'):
            if row['owner_id'] != target and mentions(row):
                blockers.append('identifier_provenance')
        if self.db.execute('SELECT 1 FROM migrations LIMIT 1').fetchone() or self.db.execute('SELECT 1 FROM legacy_map LIMIT 1').fetchone():
            blockers.append('immutable_legacy_backup_inventory_required')
        if any(mentions(row) for row in self.db.execute("SELECT * FROM audit WHERE event='metadata_correction'")):
            blockers.append('immutable_metadata_history')
        jobs = [dict(row) for row in self.db.execute('SELECT * FROM jobs ORDER BY id') if mentions(row)]
        if any(row['kind'] != 'index' or json.loads(row['payload']).get('version_id') not in version_ids for row in jobs):
            blockers.append('ambiguous_job_ownership')
        if any(row['state'] not in ('succeeded', 'failed', 'cancelled') or row['lease_token'] for row in jobs):
            blockers.append('active_job_or_generation')
        assets = [dict(row) for row in self.db.execute('SELECT * FROM version_assets WHERE version_id IN (SELECT value FROM json_each(?)) ORDER BY version_id,hash,role', (encoded(version_ids),))]
        hashes = sorted({row['hash'] for row in assets})
        blobs = [dict(row) for row in self.db.execute('SELECT * FROM blobs WHERE hash IN (SELECT value FROM json_each(?)) ORDER BY hash', (encoded(hashes),))]
        if not versions or {row['version_id'] for row in assets} != set(version_ids) or len(blobs) != len(hashes):
            blockers.append('incomplete_original_ownership_inventory')
        for content_hash in hashes:
            if self.db.execute('SELECT 1 FROM version_assets WHERE hash=? AND version_id NOT IN (SELECT value FROM json_each(?))', (content_hash, encoded(version_ids))).fetchone():
                blockers.append('shared_blob')
        require(not blockers, 'PURGE_BLOCKED', 'permanent deletion blocked: ' + ','.join(sorted(set(blockers))))
        try:
            for blob in blobs:
                require(re.fullmatch('[0-9a-f]{64}', blob['hash']) is not None and blob['path'] == 'objects/sha256/' + blob['hash'][:2] + '/' + blob['hash'],
                        'PURGE_UNSAFE_PATH', 'only canonical content-addressed owned paths can be purged')
                with directory(self.root / 'objects' / 'sha256' / blob['hash'][:2], self.sandbox_root) as parent:
                    blob.update(inspect_file(parent, blob['hash'], blob))
        except OSError as error:
            raise CorpusError('PURGE_UNSAFE_PATH', 'cannot safely inspect owned object') from error
        contexts = [dict(row) for row in self.db.execute('SELECT * FROM context_links WHERE target_id IN (SELECT value FROM json_each(?)) ORDER BY context_id,target_id', (encoded(identifiers),))]
        owned_identifiers = [dict(row) for row in self.db.execute('SELECT * FROM external_identifiers WHERE owner_id=? ORDER BY scheme,raw_value', (target,))]
        active = [dict(row) for row in self.db.execute('SELECT * FROM active_generations WHERE version_id IN (SELECT value FROM json_each(?)) ORDER BY version_id', (encoded(version_ids),))]
        return {'fingerprint': digest({'work': dict(work), 'versions': versions, 'generations': generations,
                    'chunks': chunks, 'assets': assets, 'blobs': blobs, 'jobs': jobs, 'contexts': contexts,
                    'identifiers': owned_identifiers, 'active_generations': active}),
                'versions': version_ids, 'generations': generation_ids, 'chunks': [row['id'] for row in chunks],
                'jobs': [row['id'] for row in jobs], 'identifiers': identifiers, 'blobs': blobs,
                'policy': 'block_known_evidence_aliases_shared_bytes_active_jobs_and_legacy_backups; retain_noncontent_identity_tombstones',
                'unknown_external_references': 'not_enumerable; old_unregistered_refs_return_explicit_purge_tombstones',
                'erasure': 'application_level_only; SQLite_free_pages_WAL_filesystem_snapshots_and_external_backups_not_securely_erased'}

    def _purge_redact(self, target, inventory):
        self.require_sensitive_actions()
        for table in ('source_versions', 'generations', 'chunks'):
            self.db.execute(f'DROP TRIGGER immutable_{table}_UPDATE')
        for table in ('version_assets', 'blobs'):
            self.db.execute(f'DROP TRIGGER immutable_{table}_DELETE')
        try:
            self.db.execute("UPDATE works SET lifecycle='tombstone',title='',metadata='{}',preferred_version=NULL,revision=revision+1 WHERE id=?", (target,))
            for version in inventory['versions']:
                self.db.execute('DELETE FROM active_generations WHERE version_id=?', (version,))
                self.db.execute('DELETE FROM version_assets WHERE version_id=?', (version,))
                self.db.execute("UPDATE source_versions SET metadata='{}' WHERE id=?", (version,))
            for generation in inventory['generations']:
                self.db.execute("UPDATE generations SET canonical='',warnings='[]' WHERE id=?", (generation,))
            for chunk in inventory['chunks']:
                self.db.execute('DELETE FROM chunk_fts WHERE chunk_id=?', (chunk,))
                self.db.execute('DELETE FROM chunk_embeddings WHERE chunk_id=?', (chunk,))
                self.db.execute('DELETE FROM legacy_vectors WHERE chunk_id=?', (chunk,))
                self.db.execute("UPDATE chunks SET text='',locator='{}' WHERE id=?", (chunk,))
            for job in inventory['jobs']:
                self.db.execute('DELETE FROM job_parse_cache WHERE job_id=?', (job,))
                self.db.execute('DELETE FROM job_steps WHERE job_id=?', (job,))
                self.db.execute('DELETE FROM jobs WHERE id=?', (job,))
            self.db.execute('DELETE FROM context_links WHERE target_id IN (SELECT value FROM json_each(?))', (encoded(inventory['identifiers']),))
            self.db.execute('DELETE FROM external_identifiers WHERE owner_id=?', (target,))
            for blob in inventory['blobs']:
                self.db.execute('DELETE FROM blobs WHERE hash=?', (blob['hash'],))
        finally:
            for table in ('source_versions', 'generations', 'chunks'):
                self.db.execute(f"CREATE TRIGGER immutable_{table}_UPDATE BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'immutable content'); END")
            for table in ('version_assets', 'blobs'):
                self.db.execute(f"CREATE TRIGGER immutable_{table}_DELETE BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'immutable content'); END")

    def apply_purge(self, proposal_id, approval_ref, operation_id, request):
        self.require_sensitive_actions()
        with transaction(self.db):
            previous = self.receipt(operation_id, request)
            if previous is None:
                frozen = self._check_approval(proposal_id, approval_ref)
                inventory = self.purge_inventory(frozen['target'])
                require(inventory == frozen['purge_inventory'], 'IMPACT_CHANGED', 'purge dependencies or owned bytes changed; repropose')
                require(self.db.execute('SELECT revision FROM works WHERE id=?', (frozen['target'],)).fetchone()[0] == frozen['expected_revision'], 'REVISION_CONFLICT', 'work revision changed')
                manifest = inventory['blobs']
                self.db.execute('INSERT INTO purge_journal VALUES (?,?,?,?,?)', (operation_id, frozen['target'], encoded(manifest), 'cleanup_pending', now()))
                self._purge_redact(frozen['target'], inventory)
                result = {'state': 'cleanup_pending', 'target': frozen['target'], 'operation_id': operation_id,
                          'blobs_deleted': 0, 'secure_erasure': False, 'historical_backups': 'not_erased_or_claimed; legacy_backed_candidates_blocked'}
                self.db.execute('INSERT INTO receipts VALUES (?,?,?)', (operation_id, digest(request), encoded(result)))
                self.db.execute('UPDATE approvals SET consumed=1 WHERE id=?', (approval_ref,))
                self.db.execute("UPDATE proposals SET state='applied' WHERE id=?", (proposal_id,))
                audit(self.db, 'purge_intent', {'target': frozen['target'], 'operation_id': operation_id, 'blob_count': len(manifest)})
                self._purge_checkpoint('before_intent_commit')
        self._purge_checkpoint('after_intent_commit')
        return self.recover_purge(operation_id)

    def recover_purge(self, operation_id):
        self.require_sensitive_actions()
        try:
            with transaction(self.db):
                journal = self.db.execute('SELECT * FROM purge_journal WHERE operation_id=?', (operation_id,)).fetchone()
                require(journal is not None, 'NOT_FOUND', 'purge intent missing')
                result = json.loads(self.db.execute('SELECT result FROM receipts WHERE operation_id=?', (operation_id,)).fetchone()[0])
                if journal['state'] == 'purged':
                    return result
                manifest = json.loads(journal['manifest'])
                with directory(self.root, self.sandbox_root) as root_fd:
                    try:
                        os.mkdir('.purge', mode=0o700, dir_fd=root_fd)
                        os.fsync(root_fd)
                    except FileExistsError:
                        pass
                    quarantine = os.open('.purge', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                    try:
                        require(os.fstat(quarantine).st_mode & 0o077 == 0, 'PURGE_UNSAFE_PATH', 'purge staging must be private')
                        for blob in manifest:
                            require(not self.db.execute('SELECT 1 FROM blobs WHERE hash=?', (blob['hash'],)).fetchone(), 'PURGE_BLOCKED', 'object reattached during pending purge')
                            staged = digest({'operation': operation_id, 'hash': blob['hash']})
                            with directory(self.root / 'objects' / 'sha256' / blob['hash'][:2], self.sandbox_root) as parent:
                                try:
                                    status = inspect_file(quarantine, staged, blob)
                                except FileNotFoundError:
                                    try:
                                        status = inspect_file(parent, blob['hash'], blob)
                                    except FileNotFoundError:
                                        os.fsync(parent)
                                        os.fsync(quarantine)
                                        continue
                                    require(status == {key: blob[key] for key in ('device', 'inode')}, 'PURGE_UNSAFE_PATH', 'owned object identity changed')
                                    os.rename(blob['hash'], staged, src_dir_fd=parent, dst_dir_fd=quarantine)
                                    os.fsync(parent)
                                    os.fsync(quarantine)
                                require(inspect_file(quarantine, staged, blob) == {key: blob[key] for key in ('device', 'inode')}, 'PURGE_UNSAFE_PATH', 'staged object identity changed')
                                self._purge_checkpoint('after_stage')
                                os.unlink(staged, dir_fd=quarantine)
                                os.fsync(quarantine)
                                self._purge_checkpoint('after_unlink')
                                try:
                                    os.stat(blob['hash'], dir_fd=parent, follow_symlinks=False)
                                except FileNotFoundError:
                                    pass
                                else:
                                    raise CorpusError('PURGE_UNSAFE_PATH', 'object path was replaced during cleanup; operator inventory required')
                    finally:
                        os.close(quarantine)
                result.update(state='purged', blobs_deleted=len(manifest))
                result.pop('cleanup_error', None)
                self.db.execute("UPDATE purge_journal SET state='purged' WHERE operation_id=?", (operation_id,))
                self.db.execute('UPDATE receipts SET result=? WHERE operation_id=?', (encoded(result), operation_id))
                audit(self.db, 'purge_complete', {'target': journal['target'], 'operation_id': operation_id, 'blobs_deleted': len(manifest)})
                self._purge_checkpoint('before_complete_commit')
                return result
        except (OSError, CorpusError) as error:
            with transaction(self.db):
                row = self.db.execute('SELECT result FROM receipts WHERE operation_id=?', (operation_id,)).fetchone()
                if row is None:
                    raise
                result = json.loads(row[0])
                if result['state'] != 'purged':
                    result['cleanup_error'] = getattr(error, 'code', type(error).__name__)
                    self.db.execute('UPDATE receipts SET result=? WHERE operation_id=?', (encoded(result), operation_id))
                return result

    def purge_tombstone(self, ref):
        segments = ref.removeprefix('corpus://').split('/')
        if len(segments) != 2:
            return None
        kind, target = segments
        queries = {'work': 'SELECT id FROM works WHERE id=? AND lifecycle=\'tombstone\'',
                   'version': 'SELECT w.id FROM works w JOIN source_versions v ON v.work_id=w.id WHERE v.id=? AND w.lifecycle=\'tombstone\'',
                   'chunk': 'SELECT w.id FROM works w JOIN source_versions v ON v.work_id=w.id JOIN generations g ON g.version_id=v.id JOIN chunks c ON c.generation_id=g.id WHERE c.id=? AND w.lifecycle=\'tombstone\''}
        row = self.db.execute(queries[kind], (target,)).fetchone() if kind in queries else None
        if row:
            journal = self.db.execute('SELECT state,operation_id FROM purge_journal WHERE target=?', (row[0],)).fetchone()
            return {'ref': ref, 'target': row[0], 'state': journal['state'], 'operation_id': journal['operation_id'],
                    'content_available': False, 'warning': 'explicit_permanent_deletion; not_source_evidence'}
