from .contracts import validate
from .errors import require


def dispatch(corpus, tool, args):
    action = validate(tool, args)
    if tool == 'corpus_ingest':
        return corpus.preflight(args['inputs']) if action == 'preflight' else corpus.ingest({key: value for key, value in args.items() if key != 'action'})
    if tool == 'corpus_search':
        if action == 'overview':
            return corpus.overview(args['context'], args.get('limit', 20), args.get('cursor'))
        if action == 'research':
            return corpus.search_research(args['query'], args.get('context'), args.get('limit', 20), args.get('cursor'))
        if action == 'similar':
            source = corpus.read(args['ref'])
            text = source.get('chunk', {}).get('text')
            require(text is not None, 'INVALID_REF', 'similar currently requires a chunk URI')
            result = corpus.search(text[:2000], top_k=args.get('top_k', 10), cursor=args.get('cursor'))
            if corpus.embedding is None or not result['vector_candidates']:
                result['warnings'].append('similar_uses_lexical_not_semantic_without_configured_provider')
            result['similarity_layer'] = 'configured_source_evidence' if result['vector_candidates'] else 'lexical_source_evidence_degraded'
            result['hierarchy'] = 'flat_chunks_no_verified_same_level_constraint'
            return result
        return corpus.search(args['query'], args.get('scope', 'global'), args.get('context'), args.get('top_k', 10), args.get('cursor'),
                             filters=args.get('filters'), candidate_budget=args.get('candidate_budget', 10000))
    if tool == 'corpus_read':
        if action == 'history':
            return corpus.history(args['ref'], args.get('limit', 20), args.get('cursor'))
        if action == 'resolve':
            if args['ref'].startswith('corpus://'):
                return corpus.read(args['ref'], args.get('use_context', 'browse'))
            return corpus.resolve_legacy(args['ref'])
        require('cursor' not in args and 'limit' not in args, 'INVALID_ARGUMENT', 'pagination only supported by history view')
        if action in ('chunk', 'version'):
            require(args['ref'].startswith('corpus://' + action + '/'), 'INVALID_REF', 'view and reference kind mismatch')
        if action == 'chunk' and ('before' in args or 'after' in args):
            return corpus.expand_chunk(args['ref'], args.get('before', 1), args.get('after', 1), args.get('use_context', 'browse'))
        return corpus.read(args['ref'], args.get('use_context', 'browse'))
    if tool == 'corpus_reference':
        if action == 'save_draft':
            return corpus.save_draft({key: value for key, value in args.items() if key != 'action'})
        if action == 'propose':
            return corpus.propose_publish(args['revision_id'], args['operation_id'])
        if action == 'propose_batch':
            return corpus.propose_batch(args['items'], args['operation_id'])
        if action == 'reject':
            return corpus.reject(args['proposal_id'], args['operation_id'])
        return corpus.apply_proposal(args['proposal_id'], args['approval_ref'], args['operation_id'], domain='reference')
    if tool == 'corpus_jobs':
        if action == 'status':
            return corpus.job(args['job_id'])
        if action == 'list':
            return corpus.list_jobs(args.get('state'), args.get('limit', 20), args.get('cursor'))
        return corpus.jobs_control(action, args['job_id'], args['operation_id'])
    if action == 'link':
        return corpus.link(args['target'], args['context'], args['operation_id'])
    if action == 'external_link':
        return corpus.external_link(args['target'], args['uri'], args['basis'], args['operation_id'])
    if action in ('metadata', 'assess'):
        return corpus.metadata(args['target'], args['expected_revision'], args['assessment' if action == 'assess' else 'changes'], args['source_ref'], args['operation_id'], action == 'assess')
    if action == 'review':
        return corpus.review(args['target'], args['scope'], args['source_ref'], args['conclusion'], args['remaining'], args['operation_id'])
    if action in ('recycle', 'restore'):
        return corpus.lifecycle(args['target'], 'recycled' if action == 'recycle' else 'active', args['expected_revision'], args['operation_id'])
    if action == 'propose':
        return corpus.propose_maintenance(args)
    return corpus.apply_proposal(args['proposal_id'], args['approval_ref'], args['operation_id'], domain='manage')
