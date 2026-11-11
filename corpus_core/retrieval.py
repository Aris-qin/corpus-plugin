import heapq
import json
import re

from .db import encoded, transaction
from .embeddings import cosine
from .errors import require
from .paging import page, validate_cursor


class Retrieval:
    def index_vectors(self, profile, provider, test_root=None, budget=100):
        require(profile.provider != 'mock' or self.test_mode, 'MOCK_FORBIDDEN', 'production-mode candidate refuses mock profiles')
        if test_root is not None:
            require(self.root.resolve().is_relative_to(test_root.resolve()), 'MOCK_FORBIDDEN', 'test root mismatch')
        profile.validate(test_root)
        rows = self.db.execute('SELECT c.id,c.text FROM chunks c JOIN active_generations a ON a.generation_id=c.generation_id ORDER BY c.id LIMIT ?', (budget + 1,)).fetchall()
        require(len(rows) <= budget, 'NEEDS_SCOPE_APPROVAL', 'vector batch exceeds explicit budget')
        require(not any(self.db.execute("SELECT 1 FROM legacy_map WHERE table_name='chunks' AND target_id=?", ('corpus://chunk/' + row['id'],)).fetchone() for row in rows),
                'LEGACY_REEMBEDDING_FORBIDDEN', 'legacy vectors require evidence-bound profile claim; reembedding is not authorized')
        vectors = provider([row['text'] for row in rows])
        require(len(vectors) == len(rows), 'INVALID_VECTOR', 'provider count mismatch')
        for vector in vectors:
            profile.vector(vector)
        with transaction(self.db):
            self.db.execute('INSERT OR IGNORE INTO embedding_profiles VALUES (?,?)', (profile.id, encoded(profile.config)))
            for row, vector in zip(rows, vectors):
                self.db.execute('INSERT OR IGNORE INTO chunk_embeddings VALUES (?,?,?)', (row['id'], profile.id, encoded(vector)))
        return {'indexed': len(rows), 'profile_id': profile.id, 'backend': 'stdlib_exact_bounded'}

    def search(self, query, scope='global', context=None, top_k=10, cursor=None,
               profile=None, query_vector=None, candidate_budget=10000, filters=None):
        from .contracts import FILTERS, validate_value
        filters = {} if filters is None else filters
        validate_value(filters, FILTERS, 'filters')
        require(type(query) is str and 0 < len(query) <= 2000, 'INVALID_QUERY', 'query length 1..2000 required')
        require(scope in ('global', 'linked'), 'INVALID_SCOPE', 'invalid scope')
        require(scope != 'linked' or context is not None, 'CONTEXT_REQUIRED', 'linked scope requires explicit context')
        require(type(candidate_budget) is int and 1 <= candidate_budget <= 10000, 'INVALID_BUDGET', 'candidate budget 1..10000')
        require(type(top_k) is int and 1 <= top_k <= 100, 'INVALID_LIMIT', 'limit must be integer 1..100')
        parameters = []
        condition = ''
        if scope == 'linked':
            condition = " AND EXISTS(SELECT 1 FROM context_links l JOIN works linked ON linked.id=l.target_id WHERE (linked.id=cw.id OR json_extract(linked.metadata,'$.merged_into')=cw.id) AND l.context_id=?)"
            parameters.append(context)
        columns = {'work_ids': 'cw.id', 'version_ids': 'v.id', 'version_kinds': 'v.version_kind', 'kinds': 'cw.kind', 'coverage': 'v.coverage'}
        for field, column in columns.items():
            if field in filters:
                condition += ' AND ' + column + ' IN (SELECT value FROM json_each(?))'
                parameters.append(encoded(filters[field]))
        if 'retraction_status' in filters:
            condition += " AND (coalesce(json_extract(w.metadata,'$.retraction_status.value'),'unknown') IN (SELECT value FROM json_each(?)) OR coalesce(json_extract(cw.metadata,'$.retraction_status.value'),'unknown') IN (SELECT value FROM json_each(?)))"
            parameters.extend([encoded(filters['retraction_status'])] * 2)
        for field, operator in (('year_min', '>='), ('year_max', '<=')):
            if field in filters:
                condition += " AND json_extract(cw.metadata,'$.year.value') " + operator + ' ?'
                parameters.append(filters[field])
        if 'doi' in filters:
            from .identity import normalize_identifier
            condition += " AND EXISTS(SELECT 1 FROM external_identifiers e WHERE e.owner_id IN (w.id,cw.id) AND e.scheme='doi' AND e.lookup_key=?)"
            parameters.append(normalize_identifier('doi', filters['doi']))
        eligible = ('SELECT c.*,g.version_id,cw.id work_id,v.work_id original_work_id,v.coverage,v.version_kind,(cw.preferred_version=v.id) preferred FROM chunks c '
                               'JOIN active_generations a ON a.generation_id=c.generation_id '
                               'JOIN generations g ON g.id=c.generation_id JOIN source_versions v ON v.id=g.version_id '
                               "JOIN works w ON w.id=v.work_id JOIN works cw ON cw.id=coalesce(json_extract(w.metadata,'$.merged_into'),w.id) WHERE cw.lifecycle='active' AND (w.lifecycle='active' OR json_extract(w.metadata,'$.merged_into') IS NOT NULL)" + condition)
        tokens = re.findall(r'\w+', query.lower())
        fts = iter(())
        if tokens:
            expression = ' OR '.join('"' + token.replace('"', '""') + '"' for token in tokens)
            fts = iter(self.db.execute('SELECT chunk_id,-bm25(chunk_fts) score FROM chunk_fts WHERE chunk_fts MATCH ? ORDER BY chunk_id', (expression,)))
        diagnostics = {'coverage': 'full_eligible_corpus', 'eligible_chunks_scanned': 0,
                       'lexical_matches': 0, 'vectors_scanned': 0, 'ranking_heap_limit': candidate_budget}

        def lexical_candidates():
            match = next(fts, None)
            for row in self.db.execute(eligible + ' ORDER BY c.id', parameters):
                diagnostics['eligible_chunks_scanned'] += 1
                while match is not None and match['chunk_id'] < row['id']:
                    match = next(fts, None)
                score = sum(token in row['text'].lower() for token in tokens)
                if match is not None and match['chunk_id'] == row['id']:
                    score += match['score']
                if score > 0:
                    diagnostics['lexical_matches'] += 1
                    yield row['id'], score, row['version_kind'] != 'formal', not row['preferred']

        ranking_key = lambda pair: (-pair[1], pair[0])
        selection_key = lambda pair: (pair[2], pair[3], -pair[1], pair[0])
        lexical = sorted([(pair[0], pair[1]) for pair in heapq.nsmallest(candidate_budget, lexical_candidates(), key=selection_key)], key=ranking_key)
        if hasattr(fts, 'close'):
            fts.close()
        vector_scores = []
        warnings = ['lexical_only_no_embedding_requested']
        incomplete_index = self.db.execute("SELECT 1 FROM jobs WHERE state='partial' LIMIT 1").fetchone() is not None
        configured_status = None
        if profile is None and self.embedding is not None and diagnostics['eligible_chunks_scanned']:
            query_vector, configured_status = self.embedding.query(query)
            if query_vector is not None:
                profile = self.embedding.profile
        if profile is not None:
            require(profile.provider != 'mock' or self.test_mode, 'MOCK_FORBIDDEN', 'production-mode candidate refuses mock query profiles')
            profile.vector(query_vector)
            known = self.db.execute('SELECT config FROM embedding_profiles WHERE id=?', (profile.id,)).fetchone()
            if known:
                embeddings = self.db.execute('SELECT e.chunk_id,e.vector,eligible.version_kind,eligible.preferred FROM chunk_embeddings e JOIN (' + eligible + ') eligible ON eligible.id=e.chunk_id WHERE e.profile_id=?', (*parameters, profile.id))

                def vector_candidates():
                    for row in embeddings:
                        diagnostics['vectors_scanned'] += 1
                        yield row['chunk_id'], cosine(query_vector, json.loads(row['vector'])), row['version_kind'] != 'formal', not row['preferred']

                vector_scores = sorted([(pair[0], pair[1]) for pair in heapq.nsmallest(candidate_budget, vector_candidates(), key=selection_key)], key=ranking_key)
                warnings = ['exact_vector_scan_full_eligible_linear_cost_bounded_memory']
            else:
                warnings = ['search_degraded_profile_not_indexed']
        if configured_status is not None and configured_status != 'complete':
            warnings = ['lexical_fallback', 'embedding_' + configured_status]
        if incomplete_index:
            warnings.append('index_incomplete')
        if self.db.execute("SELECT 1 FROM legacy_vectors WHERE profile='legacy_unknown' LIMIT 1").fetchone():
            warnings.append('legacy_vectors_quarantined_unverified_provenance')
        combined = {}
        for ranking in (lexical, vector_scores):
            for rank, (chunk_id, score) in enumerate(ranking, 1):
                combined[chunk_id] = combined.get(chunk_id, 0) + 1 / (60 + rank)
        by_id = {row['id']: row for row in self.db.execute(eligible + ' AND c.id IN (SELECT value FROM json_each(?))', (*parameters, encoded(sorted(combined))))}
        ordered = sorted(combined, key=lambda key: (by_id[key]['version_kind'] != 'formal', not by_id[key]['preferred'], -combined[key], key))
        truncated = (diagnostics['lexical_matches'] > candidate_budget or
                     diagnostics['vectors_scanned'] > candidate_budget or len(ordered) > candidate_budget)
        diagnostics['candidate_union_count'] = len(ordered)
        ordered = ordered[:candidate_budget]
        hits = []
        per_version = {}
        lexical_scores = dict(lexical)
        vector_by_id = dict(vector_scores)
        for chunk_id in ordered:
            row = by_id[chunk_id]
            if per_version.get(row['version_id'], 0) >= 5:
                continue
            per_version[row['version_id']] = per_version.get(row['version_id'], 0) + 1
            hits.append({'ref': 'corpus://chunk/' + chunk_id, 'version_id': row['version_id'],
                         'work_id': row['work_id'], 'coverage': row['coverage'], 'text': row['text'],
                         'original_work_id': row['original_work_id'], 'version_kind': row['version_kind'], 'preferred': bool(row['preferred']),
                         **self.source_evidence(row['version_id'], 'corpus://chunk/' + chunk_id),
                         'locator': json.loads(row['locator']), 'rrf_score': combined[chunk_id],
                         'lexical_score': lexical_scores.get(chunk_id), 'vector_score': vector_by_id.get(chunk_id)})
        binding = {'query': query, 'scope': scope, 'context': context, 'top_k': top_k,
                   'profile': profile.id if profile else None, 'query_vector': query_vector, 'budget': candidate_budget, 'filters': filters}
        if self.rerank is not None:
            binding['rerank'] = self.rerank.id
            if cursor is not None:
                validate_cursor(self.root, binding, cursor)
            hits, rerank_warnings = self.rerank.rank(query, hits, binding)
            warnings.extend(rerank_warnings)
        selected, next_cursor = page(self.root, hits, binding, top_k, cursor)
        incomplete = self.db.execute("SELECT count(*) FROM jobs WHERE state!='succeeded'").fetchone()[0]
        return {'hits': selected, 'cursor': next_cursor, 'scope': scope, 'truncated': truncated,
                'related_reports': self.related_reports(sorted({hit['version_id'] for hit in selected})),
                'vector_candidates': len(vector_scores), 'candidate_count': len(ordered), 'warnings': warnings,
                'retrieval_diagnostics': diagnostics,
                'status': 'candidate_budget_exhausted' if truncated else ('matching_results' if hits else ('processing_incomplete' if incomplete else 'no_matching_results'))}
