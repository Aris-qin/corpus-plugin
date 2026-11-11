"""Parent-operated opt-in smoke; no credentials, production data, or retries."""
import argparse
import json
import os
from urllib.parse import urlsplit

from .errors import CorpusError, require
from .paths import bounded, sandbox_root
from .rerank import RerankRuntime
from .service import Corpus
from .settings import EmbeddingRuntime, load_settings


def bounded_smoke(settings_path, authorized_root):
    boundary = sandbox_root(authorized_root)
    settings = load_settings(settings_path, boundary)
    embedding, rerank = settings.get('embedding'), settings.get('rerank')
    require(type(embedding) is dict and type(rerank) is dict, 'SMOKE_CONFIG', 'both runtimes require explicit configuration')
    profile, policy = embedding.get('profile', {}), embedding.get('policy', {})
    require(profile.get('exact_model') == 'text-embedding-v4' and profile.get('dim') == 1024 and profile.get('provider') == 'dashscope',
            'SMOKE_CONFIG', 'smoke requires dashscope text-embedding-v4 at 1024 dimensions')
    require(policy.get('operation_ids') == ['live-smoke'] and policy.get('max_calls') == 1 and
            policy.get('batch_size') == 1 and type(policy.get('max_chars')) is int and 1 <= policy['max_chars'] <= 2048,
            'SMOKE_CONFIG', 'embedding smoke policy requires one bounded request only')
    require(rerank.get('exact_model') == 'qwen3-rerank' and rerank.get('protocol') == 'dashscope_native' and
            rerank.get('max_calls') == 1 and rerank.get('max_documents') == 3 and rerank.get('candidate_budget') == 3 and
            type(rerank.get('max_chars')) is int and 1 <= rerank['max_chars'] <= 2048,
            'SMOKE_CONFIG', 'rerank smoke requires native qwen3-rerank, one request and three short documents')
    for configuration, path in ((embedding, '/compatible-mode/v1/embeddings'), (rerank, '/api/v1/services/rerank/text-rerank')):
        endpoint = urlsplit(configuration.get('endpoint', ''))
        require(endpoint.scheme == 'https' and endpoint.hostname in ('dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com') and
                endpoint.path == path and not endpoint.query and not endpoint.fragment and not endpoint.username and endpoint.port in (None, 443),
                'SMOKE_CONFIG', 'explicit DashScope endpoint required')
    root = bounded(settings['data_root'], boundary)
    require(root != boundary and not root.exists(), 'SMOKE_ROOT', 'smoke requires a fresh isolated data root; never reopen or retry')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    corpus = Corpus(root, sandbox_root=boundary)
    try:
        corpus.embedding = EmbeddingRuntime(corpus, embedding)
        corpus.rerank = RerankRuntime(corpus, rerank)
        vectors, status = corpus.embedding.batch(['A short corpus integration smoke.'], {'operation': 'live-smoke'})
        require(status == 'complete' and vectors is not None and len(vectors) == 1 and len(vectors[0]) == 1024,
                'SMOKE_EMBEDDING_FAILED', 'embedding did not complete; no retry authorized')
        documents = ['Corpus stores original source evidence.', 'A journal publishes research papers.', 'A garden contains flowers.']
        hits = [{'text': document, 'ref': 'smoke:' + str(index), 'version_kind': 'snapshot', 'preferred': True}
                for index, document in enumerate(documents)]
        ranked, warnings = corpus.rerank.rank('Where is source evidence stored?', hits, {'operation': 'live-smoke'})
        require('rerank_complete' in warnings and len(ranked) == 3 and all('rerank_score' in hit for hit in ranked),
                'SMOKE_RERANK_FAILED', 'rerank did not complete; degraded fallback is not live success')
        return {'embedding': status, 'embedding_dimensions': len(vectors[0]), 'rerank': 'rerank_complete',
                'documents': len(ranked), 'adapter_path': 'EmbeddingRuntime_and_RerankRuntime',
                'approval_identity_verified': False, 'production_access': False}
    finally:
        corpus.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Parent-only paid smoke: one embedding and one native rerank request')
    parser.add_argument('--sandbox-root', required=True)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--execute-paid-smoke', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        require(os.environ.get('CORPUS_OFFLINE') != '1', 'NETWORK_DISABLED', 'offline process cannot execute live smoke')
        print(json.dumps(bounded_smoke(args.settings, args.sandbox_root)))
        return 0
    except (CorpusError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': getattr(error, 'code', 'SMOKE_FAILED'), 'retry_authorized': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
