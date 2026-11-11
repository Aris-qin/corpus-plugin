import json
from urllib.parse import urlsplit

from .db import audit, digest, encoded, identity, now, transaction
from .errors import CorpusError, require
from .objects import atomic_export
from .paging import page
from .paths import bounded
from .identity import identifier_conflict, normalize_identifier


class Management:
    def metadata(self, target, expected_revision, changes, source_ref, operation_id, assessment=False):
        self.evidence_target(source_ref)
        allowed = {'authors', 'year', 'title', 'doi', 'retraction_status'}
        require(type(changes) is dict and bool(changes), 'INVALID_METADATA', 'nonempty correction required')
        if assessment:
            from .contracts import ASSESSMENT, validate_value
            validate_value(changes, ASSESSMENT, 'assessment')
            require(set(changes) <= {'score', 'formula', 'uncertainty', 'basis'}, 'INVALID_ASSESSMENT', 'assessment is not a research body write channel')
        else:
            from .contracts import METADATA, validate_value
            validate_value(changes, METADATA, 'changes')
            require(set(changes) <= allowed, 'INVALID_METADATA', 'research bodies cannot be corrected as metadata')
            if 'title' in changes:
                require(type(changes['title']) is str and 0 < len(changes['title']) <= 1000, 'INVALID_METADATA', 'valid title required')

        def action():
            row = self.db.execute('SELECT * FROM works WHERE id=?', (target,)).fetchone()
            require(row is not None and row['revision'] == expected_revision, 'REVISION_CONFLICT', 'work revision changed')
            require(row['lifecycle'] != 'tombstone', 'PURGE_BLOCKED', 'permanently deleted metadata cannot be rewritten')
            metadata = json.loads(row['metadata'])
            require(not metadata.get('merged_into'), 'INVALID_MERGE', 'write canonical merge target instead')
            if not assessment and 'doi' in changes:
                key = normalize_identifier('doi', changes['doi'])
                require(not identifier_conflict(self.db, 'doi', key, target), 'IDENTITY_REVIEW_REQUIRED', 'DOI belongs to another work')
                self.db.execute("DELETE FROM external_identifiers WHERE owner_id=? AND scheme='doi'", (target,))
                self.db.execute('INSERT INTO external_identifiers VALUES (?,?,?,?,?)', (target, 'doi', changes['doi'], key, source_ref))
            if assessment:
                metadata['assessment'] = {'fields': changes, 'source_ref': source_ref, 'at': now()}
            else:
                for field, value in changes.items():
                    previous = metadata.get(field)
                    if type(previous) is not dict or previous.get('value') != value or previous.get('source_ref') != source_ref:
                        metadata[field] = {'value': value, 'source_ref': source_ref, 'at': now()}
            title = changes.get('title', row['title']) if not assessment else row['title']
            self.db.execute('UPDATE works SET title=?,metadata=?,revision=revision+1 WHERE id=?', (title, encoded(metadata), target))
            for link in self.db.execute('SELECT e.id FROM external_links e JOIN source_versions v ON e.target=\'corpus://version/\'||v.id WHERE v.work_id=?', (target,)):
                self.db.execute('INSERT INTO impact_notices VALUES (?,?,?,?)', (identity('notice'), link['id'], 'direct_source_metadata_change; not a wiki content validation', now()))
            audit(self.db, 'metadata_correction', {'target': target, 'before': dict(row), 'changes': changes, 'source_ref': source_ref})
            return {'target': target, 'revision': expected_revision + 1, 'wiki': 'not_modified'}
        return self.mutate(operation_id, {'action': 'assess' if assessment else 'metadata', 'target': target, 'revision': expected_revision, 'changes': changes, 'source_ref': source_ref}, action)

    def external_link(self, target, uri, basis, operation_id):
        self.evidence_target(target)
        require(uri.startswith(('wiki:', 'fact:')) and '..' not in uri.split('/') and len(uri) <= 2000,
                'INVALID_LINK', 'wiki relative link or fact URI required')
        require(not uri.startswith(('wiki:/', 'wiki:\\')), 'INVALID_LINK', 'wiki link must be relative')

        def action():
            self.db.execute('INSERT OR IGNORE INTO external_links VALUES (?,?,?,?,?)', (identity('link'), target, uri, basis, now()))
            return {'target': target, 'uri': uri, 'body_stored': False, 'coverage': 'explicit_link_only'}
        return self.mutate(operation_id, {'action': 'external_link', 'target': target, 'uri': uri, 'basis': basis}, action)

    def propose_maintenance(self, payload):
        operation_id = payload['operation_id']

        def action():
            target = self.db.execute('SELECT * FROM works WHERE id=?', (payload['target'],)).fetchone()
            require(target is not None and target['revision'] == payload['expected_revision'], 'REVISION_CONFLICT', 'work revision changed')
            frozen = {key: value for key, value in payload.items() if key not in ('operation_id', 'action')}
            frozen['action'] = payload['maintenance']
            if frozen['action'] == 'merge':
                require(payload.get('merge_into') and payload.get('merge_expected_revision') is not None,
                        'INVALID_MERGE', 'merge target and expected revision required')
                other = self.db.execute('SELECT * FROM works WHERE id=?', (payload['merge_into'],)).fetchone()
                require(other is not None and other['revision'] == payload['merge_expected_revision'] and other['id'] != target['id'],
                        'REVISION_CONFLICT', 'merge target revision changed')
                frozen['difference'] = {'from': dict(target), 'into': dict(other)}
                self.check_merge(target, other)
            else:
                require(not (set(payload) & {'merge_into', 'merge_expected_revision'}), 'INVALID_ARGUMENT', 'purge forbids merge fields')
                frozen['purge_inventory'] = self.purge_inventory(target['id'])
                frozen['physical_delete'] = 'permanent_owned_content_deletion_with_durable_intent_and_explicit_tombstones'
            frozen['impact'] = {'fixed_citations': 'preserved', 'unknown_external_references': 'cannot_enumerate'}
            if frozen['action'] == 'purge':
                frozen['impact']['fixed_citations'] = 'known_evidence_blocks_deletion; unknown_old_refs_resolve_to_explicit_tombstones'
            frozen['known_impact'] = self.merge_impact(target['id'], payload.get('merge_into'))
            return self._proposal(frozen)
        return self.mutate(operation_id, {'action': 'propose_maintenance', **payload}, action)

    def _apply_maintenance(self, frozen):
        self.require_sensitive_actions()
        require(frozen['known_impact'] == self.merge_impact(frozen['target'], frozen.get('merge_into')),
                'IMPACT_CHANGED', 'known relation/ref impact changed; inspect difference and repropose')
        target = self.db.execute('SELECT * FROM works WHERE id=?', (frozen['target'],)).fetchone()
        require(target is not None and target['revision'] == frozen['expected_revision'], 'REVISION_CONFLICT', 'work revision changed')
        if frozen['action'] == 'purge':
            raise CorpusError('INVALID_PROPOSAL', 'purge requires the committed-intent apply path')
        require(frozen['action'] == 'merge', 'INVALID_PROPOSAL', 'unsupported maintenance')
        other = self.db.execute('SELECT * FROM works WHERE id=?', (frozen['merge_into'],)).fetchone()
        require(other is not None and other['revision'] == frozen['merge_expected_revision'], 'REVISION_CONFLICT', 'merge target revision changed')
        require(other['lifecycle'] == 'active' and target['lifecycle'] == 'active', 'INVALID_MERGE', 'only active works can merge')
        self.check_merge(target, other)
        metadata = json.loads(target['metadata'])
        metadata['merged_into'] = other['id']
        self.db.execute("UPDATE works SET metadata=?,lifecycle='recycled',revision=revision+1 WHERE id=?", (encoded(metadata), target['id']))
        self.db.execute('UPDATE works SET revision=revision+1 WHERE id=?', (other['id'],))
        self.db.execute('INSERT OR IGNORE INTO context_links SELECT context_id,? FROM context_links WHERE target_id=?', (other['id'], target['id']))
        audit(self.db, 'merge_preserving_identities', {'from': target['id'], 'into': other['id']})
        return {'merged_from': target['id'], 'merged_into': other['id'], 'fixed_versions': 'unchanged', 'blobs_deleted': 0}

    def check_merge(self, source, target):
        require(source['id'] != target['id'] and source['lifecycle'] == target['lifecycle'] == 'active',
                'INVALID_MERGE', 'distinct active canonical works required')
        require(not json.loads(source['metadata']).get('merged_into') and not json.loads(target['metadata']).get('merged_into'),
                'INVALID_MERGE', 'merge aliases cannot be merged again')
        require(self.db.execute("SELECT 1 FROM works WHERE json_extract(metadata,'$.merged_into')=?", (source['id'],)).fetchone() is None,
                'INVALID_MERGE', 'transitive merges require explicit flattening; not supported')

    def merge_impact(self, *work_ids):
        owners = [row['id'] for row in self.db.execute("SELECT id FROM works WHERE id IN (SELECT value FROM json_each(?)) OR json_extract(metadata,'$.merged_into') IN (SELECT value FROM json_each(?)) ORDER BY id", (encoded(work_ids), encoded(work_ids)))]
        versions = [row['id'] for row in self.db.execute('SELECT id FROM source_versions WHERE work_id IN (SELECT value FROM json_each(?)) ORDER BY id', (encoded(owners),))]
        refs = ['corpus://version/' + version for version in versions]
        refs += ['corpus://chunk/' + row['id'] for row in self.db.execute('SELECT c.id FROM chunks c JOIN generations g ON g.id=c.generation_id WHERE g.version_id IN (SELECT value FROM json_each(?)) ORDER BY c.id', (encoded(versions),))]
        queries = {
            'contexts': ('SELECT * FROM context_links WHERE target_id IN (SELECT value FROM json_each(?))', owners),
            'identifiers': ('SELECT * FROM external_identifiers WHERE owner_id IN (SELECT value FROM json_each(?))', owners),
            'citations': ('SELECT * FROM citations WHERE target_version_id IN (SELECT value FROM json_each(?))', versions),
            'links': ('SELECT * FROM external_links WHERE target IN (SELECT value FROM json_each(?))', refs),
            'reviews': ('SELECT * FROM review_events WHERE target IN (SELECT value FROM json_each(?))', refs),
        }
        impact = {name: sorted([dict(row) for row in self.db.execute(query, (encoded(values),))], key=encoded)
                  for name, (query, values) in queries.items()}
        return {'fingerprint': digest({'relations': impact, 'fixed_refs': sorted(refs)}), 'relations': impact, 'versions': versions, 'fixed_refs': sorted(refs)}

    def jobs_control(self, action, job_id, operation_id):
        def execute():
            job = self.job(job_id)
            if action == 'retry':
                require(job['state'] in ('failed', 'cancelled', 'partial'), 'INVALID_JOB_STATE', 'retry only failed/cancelled/partial work')
                payload = json.loads(job['payload'])
                if payload.get('embedding_profile'):
                    require(self.embedding is not None and payload['embedding_profile'] == self.embedding.profile.id,
                            'CONFIGURATION_CHANGED', 'restore exact embedding profile before retry')
                    require(payload.get('embedding_binding') == self.embedding.binding or (payload.get('embedding_binding') is None and payload.get('embedding_policy') == self.embedding.policy_id),
                            'CONFIGURATION_CHANGED', 'endpoint, key binding and batch size cannot change during recovery; historical jobs require unchanged policy')
                    require(self.db.execute("SELECT 1 FROM embedding_batches WHERE profile_id=? AND state IN ('started','ambiguous') LIMIT 1", (self.embedding.profile.id,)).fetchone() is None,
                            'AMBIGUOUS_REVIEW_REQUIRED', 'AMBIGUOUS_REVIEW_REQUIRED: reconcile provider results via operator CLI; no blind rebilling')
                    require(payload.get('embedding_operation') in self.embedding.policy['operation_ids'], 'SCOPE_NOT_APPROVED', 'recovery requires original operation in explicit budget policy')
                    payload['embedding_policy'] = self.embedding.policy_id
                    self.db.execute('UPDATE jobs SET payload=? WHERE id=?', (encoded(payload), job_id))
                self.db.execute("UPDATE jobs SET state='queued',cancel_requested=0,error=NULL,lease_token=NULL WHERE id=?", (job_id,))
            else:
                require(job['state'] not in ('succeeded', 'cancelled'), 'INVALID_JOB_STATE', 'job already terminal')
                state = 'running' if job['state'] == 'running' else 'cancelled'
                self.db.execute('UPDATE jobs SET cancel_requested=1,state=? WHERE id=?', (state, job_id))
            return self.job(job_id)
        return self.mutate(operation_id, {'action': action, 'job_id': job_id}, execute)

    def list_jobs(self, state=None, limit=20, cursor=None):
        rows = [dict(row) for row in self.db.execute('SELECT * FROM jobs WHERE (? IS NULL OR state=?) ORDER BY id LIMIT 10001', (state, state))]
        truncated = len(rows) > 10000
        items, next_cursor = page(self.root, rows[:10000], {'kind': 'jobs', 'state': state, 'limit': limit}, limit, cursor)
        return {'items': items, 'cursor': next_cursor, 'truncated': truncated}

    def overview(self, context, limit=20, cursor=None):
        rows = [dict(row) for row in self.db.execute('SELECT r.*,v.body FROM research_records r LEFT JOIN research_revisions v ON v.id=r.current_revision_id '
                                                    'WHERE r.project_context=? OR EXISTS(SELECT 1 FROM context_links l WHERE l.target_id=r.id AND l.context_id=?) ORDER BY (r.current_revision_id IS NULL),r.id LIMIT 10001', (context, context))]
        items, next_cursor = page(self.root, rows[:10000], {'kind': 'overview', 'context': context, 'limit': limit}, limit, cursor)
        for item in items:
            revisions = [dict(row) for row in self.db.execute('SELECT id,hash,created_at FROM research_revisions WHERE record_id=? AND id IS NOT ? ORDER BY created_at DESC', (item['id'], item['current_revision_id']))]
            drafts = [row for row in revisions if self.publication_status(row['id']) == 'draft']
            superseded = [row for row in revisions if self.publication_status(row['id']) == 'superseded_formal']
            item['drafts'], item['drafts_truncated'] = drafts[:20], len(drafts) > 20
            item['superseded_formal'], item['superseded_formal_truncated'] = superseded[:20], len(superseded) > 20
        return {'items': items, 'cursor': next_cursor, 'overview_exists': any(row['kind'] == 'project_overview' for row in rows),
                'truncated': len(rows) > 10000, 'evidence_search_scope': 'global', 'reviews': 'read fixed revisions for field-scoped reviews'}

    def history(self, ref, limit=20, cursor=None):
        parts = ref.removeprefix('corpus://').split('/')
        require(len(parts) == 2 and parts[0] in ('work', 'reference'), 'INVALID_REF', 'history requires work/reference navigation URI')
        if parts[0] == 'work':
            work = self.db.execute('SELECT metadata FROM works WHERE id=?', (parts[1],)).fetchone()
            require(work is not None, 'NOT_FOUND', 'work missing')
            canonical = json.loads(work['metadata']).get('merged_into', parts[1])
            rows = [dict(row) for row in self.db.execute("SELECT v.* FROM source_versions v JOIN works w ON w.id=v.work_id WHERE w.id=? OR json_extract(w.metadata,'$.merged_into')=? ORDER BY acquired_at,v.id LIMIT 10001", (canonical, canonical))]
        else:
            rows = [dict(row) for row in self.db.execute('SELECT * FROM research_revisions WHERE record_id=? ORDER BY created_at,id LIMIT 10001', (parts[1],))]
        items, next_cursor = page(self.root, rows[:10000], {'kind': 'history', 'ref': ref, 'limit': limit}, limit, cursor)
        return {'items': items, 'cursor': next_cursor, 'truncated': len(rows) > 10000}

    def search_research(self, query, context=None, limit=20, cursor=None):
        require(type(query) is str and 0 < len(query) <= 2000, 'INVALID_QUERY', 'bounded explicit research query required')
        rows = [dict(row) for row in self.db.execute(
            'SELECT v.*,r.current_revision_id,r.kind FROM research_revisions v JOIN research_records r ON r.id=v.record_id '
            'WHERE instr(lower(v.body),lower(?))>0 AND (? IS NULL OR r.project_context=? OR EXISTS('
            'SELECT 1 FROM context_links l WHERE l.target_id=r.id AND l.context_id=?)) '
            'ORDER BY (v.id=r.current_revision_id) DESC,v.created_at DESC,v.id LIMIT 10001', (query, context, context, context))]
        for row in rows:
            row['ref'] = 'corpus://reference/' + row['record_id'] + '/revision/' + row['id']
            row['publication_status'] = self.publication_status(row['id'])
        items, next_cursor = page(self.root, rows[:10000], {'kind': 'research_search', 'query': query, 'context': context, 'limit': limit}, limit, cursor)
        return {'items': items, 'cursor': next_cursor, 'truncated': len(rows) > 10000,
                'layer': 'research_body_explicit_lookup_not_source_evidence'}

    def related_reports(self, version_ids):
        rows = self.db.execute('SELECT DISTINCT r.* FROM research_records r JOIN research_revisions v ON v.record_id=r.id '
            'JOIN citations c ON c.from_revision_id=v.id WHERE c.target_version_id IN (SELECT value FROM json_each(?)) '
            'ORDER BY (r.current_revision_id IS NULL),r.id LIMIT 101', (encoded(version_ids),)).fetchall()
        items = []
        for row in rows[:100]:
            revisions = [dict(revision) for revision in self.db.execute('SELECT id,hash,created_at FROM research_revisions WHERE record_id=? ORDER BY created_at DESC,id', (row['id'],))]
            for revision in revisions:
                revision['publication_status'] = self.publication_status(revision['id'])
                revision['ref'] = 'corpus://reference/' + row['id'] + '/revision/' + revision['id']
            items.append({**dict(row), 'current': next((revision for revision in revisions if revision['id'] == row['current_revision_id']), None),
                          'drafts': [revision for revision in revisions if revision['publication_status'] == 'draft'][:20],
                          'superseded_formal': [revision for revision in revisions if revision['publication_status'] == 'superseded_formal'][:20],
                          'revisions_truncated': len(revisions) > 20})
        return {'items': items, 'truncated': len(rows) > 100, 'basis': 'explicit_source_citations_only'}

    def export_one(self):
        row = self.db.execute("SELECT e.*,v.record_id,v.body,v.hash FROM export_outbox e JOIN research_revisions v ON v.id=e.revision_id WHERE e.state IN ('pending','failed') ORDER BY e.id LIMIT 1").fetchone()
        if row is None:
            return None
        self.require_sensitive_actions()
        try:
            directory = bounded(self.root / 'reference-exports' / row['record_id'], self.root)
            directory.mkdir(parents=True, exist_ok=True)
            data = ('---\nrevision: ' + row['revision_id'] + '\npayload_hash: ' + row['hash'] + '\n---\n\n' + row['body']).encode()
            destination = bounded(directory / (row['revision_id'] + '.md'), self.root)
            atomic_export(self.root, destination, data)
            with transaction(self.db):
                current = bounded(directory / 'current.md', self.root)
                if current.exists():
                    current_data = current.read_bytes()
                    prior = self.db.execute('SELECT id,body,hash FROM research_revisions WHERE record_id=?', (row['record_id'],)).fetchall()
                    require(any(current_data == ('---\nrevision: ' + revision['id'] + '\npayload_hash: ' + revision['hash'] + '\n---\n\n' + revision['body']).encode() for revision in prior),
                            'EXPORT_DRIFT', 'current export edited; explicit new draft import required')
                record = self.db.execute('SELECT current_revision_id FROM research_records WHERE id=?', (row['record_id'],)).fetchone()
                if record['current_revision_id'] == row['revision_id']:
                    atomic_export(self.root, current, data, replace=True)
                self.db.execute("UPDATE export_outbox SET state='succeeded',error=NULL WHERE id=?", (row['id'],))
            return {'export_id': row['id'], 'state': 'succeeded', 'path': str(destination), 'committed_data': 'unchanged'}
        except (OSError, CorpusError) as error:
            with transaction(self.db):
                self.db.execute("UPDATE export_outbox SET state='failed',error=? WHERE id=?", (getattr(error, 'code', type(error).__name__), row['id']))
            return {'export_id': row['id'], 'state': 'failed', 'committed_data': 'unchanged'}
