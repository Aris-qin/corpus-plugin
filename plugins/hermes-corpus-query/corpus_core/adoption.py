import base64
import hashlib
import json
import os
import re
import struct
from pathlib import Path

from .db import audit, digest, encoded, transaction
from .embeddings import Profile
from .errors import require
from .objects import publish, verified_blob
from .paths import bounded


class Adoption:
    def legacy_inventory(self):
        return {
            'issues': [dict(row) for row in self.db.execute('SELECT * FROM migration_issues ORDER BY source_hash,legacy_id,id')],
            'documents': [dict(row) for row in self.db.execute("SELECT m.source_hash,m.legacy_id,m.target_id,w.lifecycle,w.metadata FROM legacy_map m LEFT JOIN works w ON m.target_id='corpus://work/'||w.id WHERE m.table_name='documents' ORDER BY m.source_hash,m.legacy_id")],
            'vectors': [{key: row[key] for key in ('source_hash', 'legacy_chunk_id', 'chunk_id', 'profile')} | {'raw_vector_hash': digest(row['raw_vector'])}
                        for row in self.db.execute('SELECT * FROM legacy_vectors ORDER BY source_hash,legacy_chunk_id')],
            'unresolved_relationships': [dict(row) for row in self.db.execute('SELECT * FROM legacy_relationships WHERE related_ref IS NULL ORDER BY source_hash,table_name,legacy_id,field')],
            'vector_policy': 'unknown_remains_quarantined_no_reembedding',
            'next_steps': ['original: dry-run/adopt-original with exact snapshot/id/path/hash/size and identity evidence',
                           'vectors: dry-run/claim-legacy-profile with exact per-row hashes and injected trusted provenance verifier',
                           'classification/hierarchy/orphan: human review required; no guessed relationships'],
            'trusted_verifier_available': self.legacy_verifier is not None,
        }

    def _legacy_document(self, manifest):
        row = self.db.execute("SELECT * FROM legacy_map WHERE source_hash=? AND table_name='documents' AND legacy_id=?", (manifest['source_hash'], manifest['legacy_id'])).fetchone()
        require(row is not None and row['target_id'], 'NOT_FOUND', 'exact legacy snapshot and document mapping required')
        work_id = row['target_id'].removeprefix('corpus://work/')
        work = self.db.execute('SELECT * FROM works WHERE id=?', (work_id,)).fetchone()
        require(work is not None and work['lifecycle'] != 'tombstone', 'ADOPTION_BLOCKED', 'legacy work is unavailable')
        return row, work

    def original_adoption(self, manifest, dry_run=True):
        require(type(manifest) is dict and set(manifest) == {'source_hash', 'legacy_id', 'path', 'expected_sha256', 'expected_size', 'identity_evidence'},
                'INVALID_ADOPTION', 'strict snapshot/id/path/hash/size/identity_evidence manifest required')
        require(type(manifest['source_hash']) is str and re.fullmatch('[0-9a-f]{64}', manifest['source_hash']) and
                type(manifest['legacy_id']) is str and 0 < len(manifest['legacy_id']) <= 1000 and
                type(manifest['path']) is str and Path(manifest['path']).is_absolute() and
                type(manifest['identity_evidence']) is dict and bool(manifest['identity_evidence']),
                'INVALID_ADOPTION', 'exact snapshot hash, bounded legacy ID, absolute path and nonempty identity evidence required')
        require(type(manifest['expected_sha256']) is str and re.fullmatch('[0-9a-f]{64}', manifest['expected_sha256']), 'INVALID_ADOPTION', 'expected SHA256 required')
        require(type(manifest['expected_size']) is int and 0 <= manifest['expected_size'] <= 10 * 1024 * 1024, 'INVALID_ADOPTION', 'bounded expected size required')
        row, work = self._legacy_document(manifest)
        prior = self.db.execute('SELECT * FROM legacy_adoptions WHERE source_hash=? AND legacy_id=?', (manifest['source_hash'], manifest['legacy_id'])).fetchone()
        if prior:
            require(prior['manifest_hash'] == digest(manifest), 'IDEMPOTENCY_CONFLICT', 'different adoption manifest for same legacy document')
            blob = self.db.execute('SELECT * FROM blobs WHERE hash=?', (prior['blob_hash'],)).fetchone()
            require(blob is not None, 'OBJECT_CORRUPT', 'adopted blob metadata missing')
            verified_blob(self.root, blob)
            return {'manifest_hash': digest(manifest), 'target_id': row['target_id'], 'state': 'managed_copy', 'dry_run': dry_run,
                    'blockers': [], 'original_modified': False, 'replayed': True}
        payload = json.loads(row['raw_payload'])
        try:
            extra = json.loads(payload.get('extra') or '{}')
        except (TypeError, ValueError):
            extra = {}
        trusted_hash = extra.get('original_sha256') if type(extra) is dict else None
        identity_binding = {'manifest': manifest, 'legacy_payload_hash': digest(payload), 'target_id': row['target_id']}
        proven = trusted_hash == manifest['expected_sha256']
        if not proven and self.legacy_verifier is not None:
            proven = self.legacy_verifier('original_identity', identity_binding) is True
        blockers = [] if proven else ['original_identity_unproven: filename/size/newly supplied hash alone is not historical identity evidence']
        path = bounded(manifest['path'], self.sandbox_root)
        content = None
        if not path.is_file():
            blockers.append('missing')
        else:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                content = stream.read(10 * 1024 * 1024 + 1)
            if len(content) != manifest['expected_size'] or hashlib.sha256(content).hexdigest() != manifest['expected_sha256']:
                blockers.append('content_mismatch')
        report = {'manifest_hash': digest(manifest), 'target_id': row['target_id'], 'blockers': blockers,
                  'state': ('missing' if 'missing' in blockers else 'external_unverified') if blockers else 'verified_mapping', 'dry_run': dry_run,
                  'original_modified': False, 'identity_basis': 'preserved_original_sha256' if trusted_hash else 'injected_verifier_required'}
        if dry_run:
            return report
        require(not blockers, 'ADOPTION_BLOCKED', 'original adoption blocked: ' + encoded(blockers))
        destination = self.root / 'objects' / 'sha256' / manifest['expected_sha256'][:2] / manifest['expected_sha256']
        created = False
        try:
            with transaction(self.db):
                prior = self.db.execute('SELECT * FROM legacy_adoptions WHERE source_hash=? AND legacy_id=?', (manifest['source_hash'], manifest['legacy_id'])).fetchone()
                if prior:
                    require(prior['manifest_hash'] == digest(manifest), 'IDEMPOTENCY_CONFLICT', 'different adoption manifest for same legacy document')
                    return {**report, 'state': 'managed_copy', 'replayed': True}
                created = not destination.exists()
                blob = publish(self.root, content)
                self.db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?,?)', (blob['hash'], blob['size'], blob['path']))
                self.db.execute('INSERT INTO version_assets VALUES (?,?,?,?)', (work['preferred_version'], blob['hash'], 'legacy_original', 'application/octet-stream'))
                self.db.execute('INSERT INTO legacy_adoptions VALUES (?,?,?,?)', (manifest['source_hash'], manifest['legacy_id'], digest(manifest), blob['hash']))
                current = self.db.execute('SELECT * FROM works WHERE id=?', (work['id'],)).fetchone()
                require(current['revision'] == work['revision'], 'REVISION_CONFLICT', 'work changed during adoption; rerun dry-run')
                metadata = json.loads(current['metadata'])
                metadata['custody'] = 'managed_copy'
                metadata['original_adoption_manifest'] = digest(manifest)
                self.db.execute('UPDATE works SET metadata=?,revision=revision+1 WHERE id=?', (encoded(metadata), work['id']))
                audit(self.db, 'legacy_original_adopted', identity_binding)
            return {**report, 'state': 'managed_copy', 'replayed': False}
        except BaseException:
            if created:
                with transaction(self.db):
                    referenced = self.db.execute('SELECT 1 FROM blobs WHERE hash=? UNION SELECT 1 FROM migrations WHERE source_hash=?', (manifest['expected_sha256'], manifest['expected_sha256'])).fetchone()
                    if referenced is None:
                        bounded(destination, self.root).unlink(missing_ok=True)
            raise

    def claim_legacy_profile(self, manifest, dry_run=False):
        require(type(manifest) is dict and set(manifest) == {'source_hash', 'profile', 'vectors', 'evidence'},
                'PROVENANCE_REQUIRED', 'PROVENANCE_REQUIRED: exact per-vector hashes and trusted provenance evidence required, not dimension/model guesses')
        require(type(manifest['source_hash']) is str and re.fullmatch('[0-9a-f]{64}', manifest['source_hash']), 'PROVENANCE_REQUIRED', 'exact snapshot hash required')
        require(type(manifest['profile']) is dict and {'provider', 'exact_model', 'dim'} <= set(manifest['profile']) <= {'provider', 'exact_model', 'dim', 'metric', 'preprocessing_version', 'provenance'},
                'INVALID_PROFILE', 'strict complete profile identity required')
        profile = Profile(**manifest['profile'])
        profile.validate()
        require(profile.provider != 'mock', 'PROVENANCE_REQUIRED', 'mock legacy vectors cannot join native search')
        require(type(manifest['vectors']) is dict and bool(manifest['vectors']), 'PROVENANCE_REQUIRED', 'explicit per-vector inventory hashes required')
        require(type(manifest['evidence']) is dict and bool(manifest['evidence']), 'PROVENANCE_REQUIRED', 'nonempty provenance evidence required for independent verification')
        selected = []
        for legacy_id, expected_hash in sorted(manifest['vectors'].items()):
            require(type(legacy_id) is str and 0 < len(legacy_id) <= 1000 and type(expected_hash) is str and re.fullmatch('[0-9a-f]{64}', expected_hash),
                    'PROVENANCE_REQUIRED', 'bounded legacy vector IDs and exact inventory hashes required')
            row = self.db.execute('SELECT * FROM legacy_vectors WHERE source_hash=? AND legacy_chunk_id=?', (manifest['source_hash'], legacy_id)).fetchone()
            require(row is not None and digest(row['raw_vector']) == expected_hash, 'PROVENANCE_REQUIRED', 'exact raw vector inventory hash mismatch')
            require(row['chunk_id'] is not None, 'PROVENANCE_REQUIRED', 'orphan vectors must remain quarantined')
            raw = json.loads(row['raw_vector'])
            if type(raw) is dict and set(raw) == {'$bytes'}:
                data = base64.b64decode(raw['$bytes'], validate=True)
                require(len(data) == profile.dim * 4, 'INVALID_VECTOR', 'legacy vector dimension mismatch')
                vector = list(struct.unpack('<' + 'f' * profile.dim, data))
            else:
                vector = json.loads(raw) if type(raw) is str else raw
            profile.vector(vector)
            selected.append((row, vector))
        binding = {'manifest': manifest, 'profile_id': profile.id, 'target_chunks': [row['chunk_id'] for row, vector in selected]}
        verified = self.legacy_verifier is not None and self.legacy_verifier('vector_provenance', binding) is True
        report = {'dry_run': dry_run, 'profile_id': profile.id, 'selected': len(selected), 'blockers': [] if verified else ['trusted_per_vector_provenance_verifier_required'],
                  'unselected': 'remain_legacy_unknown', 'reembedding': False}
        if dry_run:
            return report
        require(verified, 'PROVENANCE_REQUIRED', 'trusted per-vector provenance required; dimension/comments/configured model do not prove compatibility')
        with transaction(self.db):
            self.db.execute('INSERT OR IGNORE INTO embedding_profiles VALUES (?,?)', (profile.id, encoded(profile.config)))
            changed = False
            for row, vector in selected:
                prior = self.db.execute('SELECT profile FROM legacy_vectors WHERE source_hash=? AND legacy_chunk_id=?', (row['source_hash'], row['legacy_chunk_id'])).fetchone()
                require(prior['profile'] in ('legacy_unknown', profile.id), 'PROVENANCE_REQUIRED', 'vector already claimed under a different profile')
                embedded = self.db.execute('SELECT vector FROM chunk_embeddings WHERE chunk_id=? AND profile_id=?', (row['chunk_id'], profile.id)).fetchone()
                require(embedded is None or json.loads(embedded['vector']) == vector, 'PROVENANCE_REQUIRED', 'existing embedding conflicts with recovered vector')
                if embedded is None:
                    self.db.execute('INSERT INTO chunk_embeddings VALUES (?,?,?)', (row['chunk_id'], profile.id, encoded(vector)))
                    changed = True
                if prior['profile'] != profile.id:
                    self.db.execute('UPDATE legacy_vectors SET profile=? WHERE source_hash=? AND legacy_chunk_id=?', (profile.id, row['source_hash'], row['legacy_chunk_id']))
                    changed = True
            if changed:
                audit(self.db, 'legacy_vector_profile_claimed', {'manifest_hash': digest(manifest), 'profile_id': profile.id, 'count': len(selected)})
        return report
