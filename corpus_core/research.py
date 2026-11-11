import json
import time

from .db import audit, digest, encoded, identity, now, transaction
from .errors import CorpusError, require


KINDS = {'topic_report', 'journal_profile', 'author_guidance', 'project_overview',
         'submission_assessment', 'method_comparison'}
REQUIREMENTS = {'length', 'abstract', 'sections', 'references', 'figures', 'supplements',
                'declarations', 'submission_materials', 'templates'}
MAX_EVIDENCE_DEPTH = 32
MAX_EVIDENCE_NODES = 1000


class Research:
    def evidence_target(self, ref):
        require(type(ref) is str and ref.startswith('corpus://'), 'INVALID_EVIDENCE', 'fixed corpus evidence URI required')
        require(self.purge_tombstone(ref) is None, 'INVALID_EVIDENCE', 'permanently deleted content is not evidence')
        parts = ref[9:].split('/')
        if len(parts) == 2 and parts[0] == 'version':
            require(self.db.execute('SELECT 1 FROM source_versions WHERE id=?', (parts[1],)).fetchone() is not None,
                    'INVALID_EVIDENCE', 'source version missing')
            return {'target_version_id': parts[1], 'target_revision_id': None, 'chunk_id': None, 'locator': {}}
        if len(parts) == 2 and parts[0] == 'chunk':
            row = self.db.execute('SELECT c.id,c.locator,g.version_id FROM chunks c JOIN generations g ON g.id=c.generation_id WHERE c.id=?', (parts[1],)).fetchone()
            require(row is not None, 'INVALID_EVIDENCE', 'source chunk missing')
            return {'target_version_id': row['version_id'], 'target_revision_id': None,
                    'chunk_id': row['id'], 'locator': json.loads(row['locator'])}
        if len(parts) == 4 and parts[0] == 'reference' and parts[2] == 'revision':
            row = self.db.execute('SELECT 1 FROM research_revisions WHERE id=? AND record_id=?', (parts[3], parts[1])).fetchone()
            require(row is not None, 'INVALID_EVIDENCE', 'research revision missing')
            return {'target_version_id': None, 'target_revision_id': parts[3], 'chunk_id': None, 'locator': {}}
        raise CorpusError('INVALID_EVIDENCE', 'navigation links are not immutable evidence')

    def validate_draft(self, payload):
        from .contracts import validate
        validate('corpus_reference', {'action': 'save_draft', **payload})
        required = {'kind', 'body', 'evidence', 'structured', 'change_summary', 'reason', 'operation_id'}
        require(required <= set(payload) and set(payload) <= required | {'record_id', 'parent_id', 'expected_revision'},
                'INVALID_DRAFT', 'missing or forbidden draft fields')
        require(payload['kind'] in KINDS, 'INVALID_DRAFT', 'unsupported research kind')
        for field in ('body', 'change_summary', 'reason'):
            require(type(payload[field]) is str and 0 < len(payload[field]) <= 1000000,
                    'INVALID_DRAFT', field + ' required and bounded')
        require(type(payload['evidence']) is list and len(payload['evidence']) <= 100,
                'INVALID_DRAFT', 'bounded evidence list required')
        for ref in payload['evidence']:
            self.evidence_target(ref)
        structured = payload['structured']
        require(type(structured) is dict, 'INVALID_DRAFT', 'structured object required')
        kind = payload['kind']
        if kind == 'author_guidance':
            require(type(structured.get('article_type')) is str and bool(structured['article_type']),
                    'INVALID_GUIDANCE', 'article_type required')
            require(type(structured.get('journal_id')) is str and bool(structured['journal_id']),
                    'INVALID_GUIDANCE', 'journal_id required')
            requirements = structured.get('requirements')
            require(type(requirements) is dict and set(requirements) == REQUIREMENTS,
                    'INVALID_GUIDANCE', 'all requirement categories must be explicit')
            for requirement in requirements.values():
                require(type(requirement) is dict and set(requirement) == {'status', 'source_ref', 'locator', 'checked_at', 'value'},
                        'INVALID_GUIDANCE', 'requirement fields mismatch')
                require(requirement['status'] in ('provided', 'not_provided', 'unresolved'),
                        'INVALID_GUIDANCE', 'explicit requirement status required')
                require(requirement['source_ref'] in payload['evidence'] and type(requirement['locator']) is dict,
                        'INVALID_GUIDANCE', 'requirement source must be included evidence')
                require(requirement['status'] != 'not_provided' or requirement['value'] is None,
                        'INVALID_GUIDANCE', 'not_provided cannot invent a value')
        elif kind == 'journal_profile':
            require({'journal_id', 'facts', 'assessments'} <= set(structured), 'INVALID_PROFILE', 'facts and assessments must be separate')
            for category in ('facts', 'assessments'):
                require(type(structured[category]) is list, 'INVALID_PROFILE', 'facts/assessments must be lists')
                for entry in structured[category]:
                    require(type(entry) is dict and {'source_ref', 'locator', 'checked_at', 'uncertainty'} <= set(entry),
                            'INVALID_PROFILE', 'source, locator, date and uncertainty required')
                    require(entry['source_ref'] in payload['evidence'], 'INVALID_PROFILE', 'unlisted evidence')
        elif kind == 'submission_assessment':
            require({'project_context', 'journal_id', 'fit', 'risks'} <= set(structured),
                    'INVALID_ASSESSMENT', 'project, journal, fit and risks required')
        elif kind == 'project_overview':
            require({'project_context', 'questions', 'judgements', 'disputes', 'gaps'} <= set(structured),
                    'INVALID_OVERVIEW', 'project overview sections required')

    def _draft(self, payload):
        self.validate_draft(payload)
        record_id = payload.get('record_id') or identity('reference')
        record = self.db.execute('SELECT * FROM research_records WHERE id=?', (record_id,)).fetchone()
        structured = payload['structured']
        if record:
            require(record['kind'] == payload['kind'], 'INVALID_DRAFT', 'record kind is immutable')
            require(payload.get('expected_revision') == record['revision'], 'REVISION_CONFLICT', 'record revision changed')
        else:
            require(not payload.get('record_id'), 'NOT_FOUND', 'record not found')
            project = structured.get('project_context')
            journal = structured.get('journal_id')
            if project:
                self.db.execute('INSERT OR IGNORE INTO contexts VALUES (?,?,?)', (project, 'project', '[]'))
            if journal:
                self.db.execute('INSERT OR IGNORE INTO journals VALUES (?,?,?)', (journal, 'unresolved', '[]'))
            self.db.execute('INSERT INTO research_records(id,kind,project_context,journal_id) VALUES (?,?,?,?)',
                            (record_id, payload['kind'], project, journal))
        parent = payload.get('parent_id')
        if parent:
            require(self.db.execute('SELECT 1 FROM research_revisions WHERE id=? AND record_id=?', (parent, record_id)).fetchone() is not None,
                    'INVALID_PARENT', 'parent revision does not belong to record')
        revision_id = identity('revision')
        frozen = {key: payload[key] for key in ('body', 'evidence', 'structured', 'change_summary', 'reason')}
        self.db.execute('INSERT INTO research_revisions VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (revision_id, record_id, parent, payload['body'], digest(frozen), encoded(payload['evidence']),
                         encoded(structured), payload['change_summary'], payload['reason'], now()))
        for ref in payload['evidence']:
            target = self.evidence_target(ref)
            self.db.execute('INSERT INTO citations VALUES (?,?,?,?,?,?)',
                            (identity('citation'), revision_id, target['target_version_id'], target['target_revision_id'],
                             target['chunk_id'], encoded(target['locator'])))
        if structured.get('project_context'):
            self._link(record_id, structured['project_context'])
        audit(self.db, 'draft_saved', {'record_id': record_id, 'revision_id': revision_id})
        return {'record_id': record_id, 'revision_id': revision_id, 'hash': digest(frozen), 'status': 'draft'}

    def save_draft(self, payload):
        return self.mutate(payload.get('operation_id'), {'action': 'save_draft', **payload}, lambda: self._draft(payload))

    def frozen_publication(self, revision_id):
        revision = self.db.execute('SELECT * FROM research_revisions WHERE id=?', (revision_id,)).fetchone()
        require(revision is not None, 'NOT_FOUND', 'draft revision missing')
        record = self.db.execute('SELECT * FROM research_records WHERE id=?', (revision['record_id'],)).fetchone()
        evidence = json.loads(revision['evidence'])
        require(bool(evidence), 'EVIDENCE_REQUIRED', 'formal publication requires fixed evidence')
        dependencies = self.evidence_closure(revision_id)
        return {'action': 'publish_reference', 'revision_id': revision_id, 'revision_hash': revision['hash'],
                'record_id': record['id'], 'expected_revision': record['revision'],
                'expected_current_revision': record['current_revision_id'], 'dependencies': dependencies}

    def evidence_closure(self, revision_id):
        dependencies = {}
        heights = {}
        visiting = set()

        def visit(ref, depth):
            require(depth <= MAX_EVIDENCE_DEPTH, 'EVIDENCE_LIMIT', 'evidence dependency depth limit exceeded')
            require(ref not in visiting, 'EVIDENCE_CYCLE', 'evidence dependency cycle detected')
            if ref in heights:
                require(depth + heights[ref] <= MAX_EVIDENCE_DEPTH, 'EVIDENCE_LIMIT', 'evidence dependency depth limit exceeded')
                return heights[ref]
            require(len(dependencies) < MAX_EVIDENCE_NODES, 'EVIDENCE_LIMIT', 'evidence dependency node limit exceeded')
            target = self.evidence_target(ref)
            dependencies[ref] = {'ref': ref, **target, 'state': self.dependency_state(target)}
            visiting.add(ref)
            height = 0
            if target['target_revision_id']:
                row = self.db.execute('SELECT evidence FROM research_revisions WHERE id=?', (target['target_revision_id'],)).fetchone()
                children = json.loads(row['evidence'])
                require(type(children) is list and len(children) <= 100, 'EVIDENCE_LIMIT', 'evidence list must be bounded')
                for child in children:
                    height = max(height, 1 + visit(child, depth + 1))
            visiting.remove(ref)
            heights[ref] = height
            return height

        revision = self.db.execute('SELECT record_id,evidence FROM research_revisions WHERE id=?', (revision_id,)).fetchone()
        require(revision is not None, 'NOT_FOUND', 'evidence revision missing')
        visiting.add('corpus://reference/' + revision['record_id'] + '/revision/' + revision_id)
        evidence = json.loads(revision['evidence'])
        require(type(evidence) is list and len(evidence) <= 100, 'EVIDENCE_LIMIT', 'evidence list must be bounded')
        for ref in evidence:
            visit(ref, 1)
        return [dependencies[ref] for ref in sorted(dependencies)]

    def publication_status(self, revision_id):
        row = self.db.execute('SELECT r.current_revision_id FROM research_records r JOIN research_revisions v ON v.record_id=r.id WHERE v.id=?', (revision_id,)).fetchone()
        if row and row['current_revision_id'] == revision_id:
            return 'current_formal'
        published = self.db.execute("SELECT 1 FROM audit WHERE event='reference_published' AND json_extract(payload,'$.revision_id')=?", (revision_id,)).fetchone()
        return 'superseded_formal' if published else 'draft'

    def dependency_state(self, target):
        if target['target_version_id']:
            work = self.db.execute('SELECT w.* FROM works w JOIN source_versions v ON v.work_id=w.id WHERE v.id=?', (target['target_version_id'],)).fetchone()
            state = dict(work)
            canonical = json.loads(work['metadata']).get('merged_into')
            if canonical:
                state['canonical'] = dict(self.db.execute('SELECT * FROM works WHERE id=?', (canonical,)).fetchone())
            refs = ['corpus://version/' + target['target_version_id']]
            if target['chunk_id']:
                refs.append('corpus://chunk/' + target['chunk_id'])
            state['reviews_fingerprint'] = digest(self.reviews_for(*refs))
            return state
        row = self.db.execute('SELECT r.*,v.hash FROM research_records r JOIN research_revisions v ON v.record_id=r.id WHERE v.id=?', (target['target_revision_id'],)).fetchone()
        ref = 'corpus://reference/' + row['id'] + '/revision/' + target['target_revision_id']
        return {**dict(row), 'publication_status': self.publication_status(target['target_revision_id']),
                'reviews_fingerprint': digest(self.reviews_for(ref))}

    def reviews_for(self, *refs):
        reviews = []
        for row in self.db.execute('SELECT * FROM review_events WHERE target IN (SELECT value FROM json_each(?)) ORDER BY checked_at,id', (encoded(refs),)):
            review = dict(row)
            review['scope'] = json.loads(review['scope'])
            review['remaining'] = json.loads(review['remaining'])
            reviews.append(review)
        return reviews

    def source_evidence(self, version_id, chunk_ref=None):
        work = self.db.execute('SELECT w.* FROM works w JOIN source_versions v ON v.work_id=w.id WHERE v.id=?', (version_id,)).fetchone()
        status = {**dict(work), 'metadata': json.loads(work['metadata'])}
        warnings = []
        partial = self.db.execute("SELECT checkpoint FROM jobs WHERE json_extract(payload,'$.version_id')=? AND state='partial' ORDER BY created_at DESC LIMIT 1", (version_id,)).fetchone()
        if partial:
            warnings.append('index_incomplete')
        states = [status]
        if status['metadata'].get('merged_into'):
            canonical = self.db.execute('SELECT * FROM works WHERE id=?', (status['metadata']['merged_into'],)).fetchone()
            status['canonical'] = {**dict(canonical), 'metadata': json.loads(canonical['metadata'])}
            states.append(status['canonical'])
        for state in states:
            value = state['metadata'].get('retraction_status', {})
            value = value.get('value') if type(value) is dict else value
            if value in ('retracted', 'corrected', 'expression_of_concern'):
                warnings.append('source_' + value)
            if state['lifecycle'] != 'active':
                warnings.append('source_' + state['lifecycle'])
        reviews = self.reviews_for('corpus://version/' + version_id, chunk_ref)
        return {'source_status': status, 'warnings': sorted(set(warnings)), 'reviews': reviews,
                'reminders': sorted({remaining for review in reviews for remaining in review['remaining']})}

    def _proposal(self, payload):
        proposal_id = identity('proposal')
        payload_hash = digest(payload)
        self.db.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?)',
                        (proposal_id, encoded(payload), payload_hash, 'prepared', time.time() + 86400, now()))
        binding = digest({'proposal_id': proposal_id, 'hash': payload_hash, 'payload': payload})
        audit(self.db, 'proposal_prepared', {'proposal_id': proposal_id, 'hash': payload_hash})
        return {'proposal_id': proposal_id, 'hash': payload_hash, 'binding': binding, 'payload': payload,
                'approval_channel': 'separate_operator_cli_not_identity_proof'}

    def propose_publish(self, revision_id, operation_id):
        return self.mutate(operation_id, {'action': 'propose_publish', 'revision_id': revision_id},
                           lambda: self._proposal(self.frozen_publication(revision_id)))

    def propose_batch(self, items, operation_id):
        require(type(items) is list and 1 <= len(items) <= 10, 'INVALID_BATCH', 'batch must contain 1..10 items')

        def action():
            frozen = []
            for item in items:
                require(type(item) is dict and item.get('action') in ('save_source', 'save_draft', 'publish_reference'),
                        'INVALID_BATCH', 'unknown retention action')
                if item['action'] == 'publish_reference':
                    require(set(item) == {'action', 'revision_id'}, 'INVALID_BATCH', 'publication only accepts revision_id')
                    frozen.append(self.frozen_publication(item['revision_id']))
                elif item['action'] == 'save_source':
                    require(set(item) == {'action', 'input'}, 'INVALID_BATCH', 'source item fields mismatch')
                    self.preflight([item['input']])
                    require(item['input']['source'] == 'text', 'INVALID_BATCH', 'freeze source bytes as text; file retention not yet supported')
                    frozen.append(item)
                else:
                    require(set(item) == {'action', 'draft'}, 'INVALID_BATCH', 'draft item fields mismatch')
                    self.validate_draft(item['draft'])
                    frozen.append(item)
            result = self._proposal({'action': 'retention_batch', 'items': frozen})
            for ordinal in range(len(frozen)):
                self.db.execute('INSERT INTO batch_items(proposal_id,ordinal,state) VALUES (?,?,?)', (result['proposal_id'], ordinal, 'approved_pending'))
            return result
        return self.mutate(operation_id, {'action': 'propose_batch', 'items': items}, action)

    def operator_approve(self, proposal_id, payload_hash, binding):
        self.require_sensitive_actions()
        with transaction(self.db):
            proposal = self.db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
            require(proposal is not None and proposal['state'] == 'prepared' and proposal['expires_at'] > time.time(),
                    'APPROVAL_INVALID', 'proposal not prepared or expired')
            require(proposal['hash'] == payload_hash and digest(json.loads(proposal['payload'])) == payload_hash,
                    'APPROVAL_MISMATCH', 'proposal payload hash mismatch')
            expected = digest({'proposal_id': proposal_id, 'hash': payload_hash, 'payload': json.loads(proposal['payload'])})
            require(binding == expected, 'APPROVAL_MISMATCH', 'proposal/revision binding mismatch')
            approval_ref = identity('operator')
            self.db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?)', (approval_ref, proposal_id, payload_hash, binding, 0, now()))
            self.db.execute("UPDATE proposals SET state='approved' WHERE id=?", (proposal_id,))
            audit(self.db, 'operator_cli_approval_not_identity_proof', {'proposal_id': proposal_id, 'hash': payload_hash})
            return {'approval_ref': approval_ref, 'identity_verified': False}

    def _check_approval(self, proposal_id, approval_ref):
        self.require_sensitive_actions()
        proposal = self.db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        approval = self.db.execute('SELECT * FROM approvals WHERE id=? AND proposal_id=?', (approval_ref, proposal_id)).fetchone()
        require(proposal is not None and approval is not None and not approval['consumed'] and
                proposal['state'] == 'approved' and proposal['expires_at'] > time.time(),
                'APPROVAL_INVALID', 'missing, expired, rejected or consumed operator approval')
        require(proposal['hash'] == approval['payload_hash'] == digest(json.loads(proposal['payload'])),
                'APPROVAL_MISMATCH', 'approved payload changed')
        return json.loads(proposal['payload'])

    def _publish_reference(self, frozen):
        self.require_sensitive_actions()
        revision = self.db.execute('SELECT * FROM research_revisions WHERE id=?', (frozen['revision_id'],)).fetchone()
        record = self.db.execute('SELECT * FROM research_records WHERE id=?', (frozen['record_id'],)).fetchone()
        require(revision is not None and revision['hash'] == frozen['revision_hash'], 'APPROVAL_MISMATCH', 'revision hash changed')
        require(record['revision'] == frozen['expected_revision'] and record['current_revision_id'] == frozen['expected_current_revision'],
                'REVISION_CONFLICT', 'formal record revision changed')
        current = self.evidence_closure(frozen['revision_id'])
        require(current == frozen['dependencies'], 'DEPENDENCIES_CHANGED',
                'publication dependency closure changed; repropose after difference: ' + encoded({'before': frozen['dependencies'], 'after': current}))
        for dependency in current:
            if dependency['target_version_id']:
                require(self.db.execute('SELECT 1 FROM active_generations WHERE version_id=?', (dependency['target_version_id'],)).fetchone() is not None,
                        'DEPENDENCIES_PENDING', 'source processing is incomplete')
            if dependency['target_revision_id']:
                require(self.publication_status(dependency['target_revision_id']) != 'draft', 'DEPENDENCIES_PENDING', 'research evidence is not a published revision')
        self.db.execute('UPDATE research_records SET current_revision_id=?,revision=revision+1 WHERE id=?', (revision['id'], record['id']))
        export_id = identity('export')
        self.db.execute('INSERT INTO export_outbox(id,revision_id) VALUES (?,?)', (export_id, revision['id']))
        audit(self.db, 'reference_published', {'revision_id': revision['id'], 'supersedes': record['current_revision_id']})
        return {'committed': True, 'record_id': record['id'], 'revision_id': revision['id'], 'export': 'pending', 'export_id': export_id}

    def apply_proposal(self, proposal_id, approval_ref, operation_id, domain=None):
        self.require_sensitive_actions()
        request = {'action': 'apply', 'proposal_id': proposal_id, 'approval_ref': approval_ref}
        if domain is not None:
            request['domain'] = domain
        previous = self.receipt(operation_id, request)
        if previous is not None:
            if previous.get('state') == 'cleanup_pending':
                return self.recover_purge(operation_id)
            return previous
        proposal = self.db.execute('SELECT payload FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if proposal is not None and json.loads(proposal['payload'])['action'] == 'purge':
            require(domain in (None, 'manage'), 'PROPOSAL_DOMAIN', 'purge belongs to corpus_manage')
            return self.apply_purge(proposal_id, approval_ref, operation_id, request)
        payload = self._check_approval(proposal_id, approval_ref)
        require(domain is None or (domain == 'reference' and payload['action'] in ('publish_reference', 'retention_batch')) or
                (domain == 'manage' and payload['action'] in ('merge', 'purge')),
                'PROPOSAL_DOMAIN', 'proposal domain does not match this tool')
        if payload['action'] == 'retention_batch':
            return self._apply_batch(proposal_id, approval_ref, operation_id, payload, request)

        def action():
            frozen = self._check_approval(proposal_id, approval_ref)
            if frozen['action'] == 'publish_reference':
                result = self._publish_reference(frozen)
            else:
                result = self._apply_maintenance(frozen)
            self.db.execute('UPDATE approvals SET consumed=1 WHERE id=?', (approval_ref,))
            self.db.execute("UPDATE proposals SET state='applied' WHERE id=?", (proposal_id,))
            return result
        return self.mutate(operation_id, request, action)

    def _apply_batch(self, proposal_id, approval_ref, operation_id, payload, request):
        self.require_sensitive_actions()
        for ordinal, item in enumerate(payload['items']):
            state = self.db.execute('SELECT state FROM batch_items WHERE proposal_id=? AND ordinal=?', (proposal_id, ordinal)).fetchone()['state']
            if state == 'applied':
                continue
            child_operation = f'{proposal_id}:{ordinal}'
            try:
                if item['action'] == 'save_source':
                    self._check_approval(proposal_id, approval_ref)
                    result = self.ingest({'inputs': [item['input']], 'operation_id': child_operation})
                elif item['action'] == 'save_draft':
                    self._check_approval(proposal_id, approval_ref)
                    result = self.save_draft({**item['draft'], 'operation_id': child_operation})
                else:
                    def publish_action():
                        self._check_approval(proposal_id, approval_ref)
                        return self._publish_reference(item)
                    result = self.mutate(child_operation, {'batch': proposal_id, 'item': item}, publish_action)
                with transaction(self.db):
                    self.db.execute("UPDATE batch_items SET state='applied',result=?,error=NULL WHERE proposal_id=? AND ordinal=?", (encoded(result), proposal_id, ordinal))
            except CorpusError as error:
                with transaction(self.db):
                    self.db.execute('UPDATE batch_items SET state=?,error=? WHERE proposal_id=? AND ordinal=?',
                                    ('approved_pending_dependencies' if error.code == 'DEPENDENCIES_PENDING' else 'failed', error.code, proposal_id, ordinal))

        def finish():
            self._check_approval(proposal_id, approval_ref)
            items = [dict(row) for row in self.db.execute('SELECT * FROM batch_items WHERE proposal_id=? ORDER BY ordinal', (proposal_id,))]
            done = all(item['state'] == 'applied' for item in items)
            if done:
                self.db.execute('UPDATE approvals SET consumed=1 WHERE id=?', (approval_ref,))
                self.db.execute("UPDATE proposals SET state='applied' WHERE id=?", (proposal_id,))
            return {'items': items, 'status': 'applied' if done else 'partial', 'retry': 'new operation_id for remaining approved items'}
        return self.mutate(operation_id, request, finish)

    def reject(self, proposal_id, operation_id):
        def action():
            proposal = self.db.execute('SELECT state FROM proposals WHERE id=?', (proposal_id,)).fetchone()
            require(proposal is not None and proposal['state'] in ('prepared', 'approved'), 'INVALID_PROPOSAL', 'proposal cannot be rejected')
            self.db.execute("UPDATE proposals SET state='rejected' WHERE id=?", (proposal_id,))
            return {'proposal_id': proposal_id, 'state': 'rejected'}
        return self.mutate(operation_id, {'action': 'reject', 'proposal_id': proposal_id}, action)

    def read_research(self, ref, use_context):
        parts = ref.removeprefix('corpus://').split('/')
        require(len(parts) in (2, 4) and parts[0] == 'reference', 'INVALID_REF', 'invalid research URI')
        record = self.db.execute('SELECT * FROM research_records WHERE id=?', (parts[1],)).fetchone()
        require(record is not None, 'NOT_FOUND', 'research record missing')
        revision_id = record['current_revision_id'] if len(parts) == 2 else parts[3]
        require(len(parts) == 2 or parts[2] == 'revision', 'INVALID_REF', 'invalid research revision URI')
        revision = self.db.execute('SELECT * FROM research_revisions WHERE id=? AND record_id=?', (revision_id, record['id'])).fetchone()
        fixed_ref = 'corpus://reference/' + record['id'] + '/revision/' + str(revision_id)
        reviews = self.reviews_for(fixed_ref)
        upstream = []
        warnings = set()
        upstream_remaining = set()
        if revision:
            for dependency in self.evidence_closure(revision_id):
                if dependency['target_version_id']:
                    item = {'ref': dependency['ref'], **self.source_evidence(dependency['target_version_id'],
                            dependency['ref'] if dependency['chunk_id'] else None)}
                    warnings.update(item['warnings'])
                    upstream_remaining.update(item['reminders'])
                else:
                    item = {'ref': dependency['ref'], 'publication_status': dependency['state']['publication_status'],
                            'reviews': self.reviews_for(dependency['ref'])}
                    if item['publication_status'] != 'current_formal':
                        warnings.add('upstream_research_' + item['publication_status'])
                    for review in item['reviews']:
                        upstream_remaining.update(review['remaining'])
                upstream.append(item)
        reminders = []
        if use_context in ('report', 'submission'):
            reminders.append('verify_official_sources_and_unresolved_items_for_this_use')
            if revision:
                structured = json.loads(revision['structured'])
                reminders.extend(key for key, value in structured.get('requirements', {}).items() if value['status'] != 'provided')
            for review in reviews:
                reminders.extend(review['remaining'])
            reminders.extend(sorted(upstream_remaining))
        return {'record': dict(record), 'revision': dict(revision) if revision else None,
                'formal': revision is not None and self.publication_status(revision_id) != 'draft',
                'is_current_formal': revision_id == record['current_revision_id'] and revision is not None,
                'publication_status': self.publication_status(revision_id),
                'reviews': reviews, 'reminders': reminders, 'warnings': sorted(warnings),
                'upstream_evidence': upstream, 'navigation_only': len(parts) == 2}

    def review(self, target, scope, source, conclusion, remaining, operation_id):
        self.evidence_target(target)
        self.evidence_target(source)
        require(type(scope) is list and bool(scope) and all(type(field) is str for field in scope),
                'INVALID_REVIEW', 'explicit field scope required')
        require(type(remaining) is list and all(type(field) is str for field in remaining), 'INVALID_REVIEW', 'remaining list required')

        def action():
            event_id = identity('review')
            self.db.execute('INSERT INTO review_events VALUES (?,?,?,?,?,?,?)',
                            (event_id, target, encoded(scope), source, now(), conclusion, encoded(remaining)))
            return {'review_id': event_id, 'scope': scope}
        return self.mutate(operation_id, {'action': 'review', 'target': target, 'scope': scope, 'source': source,
                                          'conclusion': conclusion, 'remaining': remaining}, action)

    def lifecycle(self, work_id, state, expected_revision, operation_id):
        require(state in ('active', 'recycled'), 'PROPOSAL_REQUIRED', 'destructive lifecycle requires proposal')

        def action():
            row = self.db.execute('SELECT revision,metadata,lifecycle FROM works WHERE id=?', (work_id,)).fetchone()
            require(row is not None and row['revision'] == expected_revision, 'REVISION_CONFLICT', 'work revision changed')
            require(row['lifecycle'] != 'tombstone', 'PURGE_BLOCKED', 'permanently deleted works cannot be restored')
            require(not json.loads(row['metadata']).get('merged_into'), 'INVALID_MERGE', 'merged aliases cannot change lifecycle')
            self.db.execute('UPDATE works SET lifecycle=?,revision=revision+1 WHERE id=?', (state, work_id))
            return {'work_id': work_id, 'lifecycle': state, 'revision': expected_revision + 1,
                    'external_references': 'unknown_references_may_exist; fixed citations preserved'}
        return self.mutate(operation_id, {'action': 'lifecycle', 'work_id': work_id, 'state': state, 'expected_revision': expected_revision}, action)
