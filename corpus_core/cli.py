import argparse
import json
import os
import time

from .contracts import CONTRACTS
from .errors import CorpusError
from .migration import migrate
from .paths import data_root
from .plugin import handler_for
from .probes import probe
from .service import Corpus


def main(argv=None):
    parser = argparse.ArgumentParser(description='Candidate corpus only; no production defaults')
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument('--root')
    location.add_argument('--settings')
    parser.add_argument('--sandbox-root', default=os.environ.get('CORPUS_AUTHORIZED_SANDBOX_ROOT'),
                        help='explicit authorized containment root; may also be set by isolated launcher environment')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('probe')
    call = subparsers.add_parser('call')
    call.add_argument('tool', choices=CONTRACTS)
    call.add_argument('--json', required=True)
    runner = subparsers.add_parser('runner')
    runner.add_argument('--max-jobs', type=int, default=10)
    runner.add_argument('--watch', action='store_true')
    operator = subparsers.add_parser('operator-approve', help='disabled: same-permission CLI is not human approval')
    operator.add_argument('--proposal-id', required=True)
    operator.add_argument('--hash', required=True)
    operator.add_argument('--binding', required=True)
    migration = subparsers.add_parser('migrate-readonly')
    migration.add_argument('--source', required=True)
    subparsers.add_parser('legacy-inventory')
    subparsers.add_parser('embedding-recovery-inventory')
    for command in ('adopt-original', 'claim-legacy-profile', 'reconcile-embedding'):
        maintenance = subparsers.add_parser(command, help='local operator evidence path; not a model tool or identity proof')
        maintenance.add_argument('--manifest', required=True)
        if command != 'reconcile-embedding':
            maintenance.add_argument('--apply', action='store_true', help='default is dry-run; evidence gates still apply')
    exporter = subparsers.add_parser('export')
    exporter.add_argument('--limit', type=int, default=10)
    subparsers.add_parser('fsck')
    args = parser.parse_args(argv)
    corpus = None
    try:
        from .paths import sandbox_root
        boundary = sandbox_root(args.sandbox_root)
        os.environ['CORPUS_AUTHORIZED_SANDBOX_ROOT'] = str(boundary)
        if args.settings:
            corpus = Corpus.from_settings(args.settings, sandbox_root=boundary)
            root = corpus.root
        else:
            root = data_root(args.root, boundary)
        if args.command == 'probe':
            result = probe(root)
        elif args.command == 'call':
            os.environ['CORPUS_SANDBOX_ROOT'] = str(root)
            if args.settings:
                os.environ['CORPUS_SETTINGS_FILE'] = args.settings
            else:
                os.environ.pop('CORPUS_SETTINGS_FILE', None)
            result = json.loads(handler_for(args.tool)(json.loads(args.json)))
        else:
            corpus = corpus or Corpus(root, sandbox_root=boundary)
            if args.command == 'operator-approve':
                result = corpus.operator_approve(args.proposal_id, args.hash, args.binding)
            elif args.command == 'migrate-readonly':
                result = migrate(corpus, args.source)
            elif args.command == 'legacy-inventory':
                result = corpus.legacy_inventory()
            elif args.command == 'embedding-recovery-inventory':
                from .errors import require
                require(corpus.embedding is not None, 'INVALID_SETTINGS', 'explicit embedding settings required')
                result = corpus.embedding.recovery_inventory()
            elif args.command in ('adopt-original', 'claim-legacy-profile', 'reconcile-embedding'):
                from .errors import require
                from .paths import bounded
                manifest_path = bounded(args.manifest, boundary)
                require(manifest_path.is_file() and manifest_path.stat().st_size <= 1024 * 1024, 'INVALID_MANIFEST', 'bounded evidence manifest required')
                manifest = json.loads(manifest_path.read_text())
                if args.command == 'adopt-original':
                    result = corpus.original_adoption(manifest, dry_run=not args.apply)
                elif args.command == 'claim-legacy-profile':
                    result = corpus.claim_legacy_profile(manifest, dry_run=not args.apply)
                else:
                    require(corpus.embedding is not None, 'INVALID_SETTINGS', 'explicit embedding settings required for reconciliation')
                    result = corpus.embedding.reconcile(manifest)
            elif args.command == 'fsck':
                from .objects import fsck
                result = fsck(corpus)
            elif args.command == 'export':
                if not 1 <= args.limit <= 100:
                    parser.error('export limit must be 1..100')
                items = []
                for iteration in range(args.limit):
                    item = corpus.export_one()
                    if item is None:
                        break
                    items.append(item)
                    if item['state'] == 'failed':
                        break
                result = {'items': items}
            else:
                if not 1 <= args.max_jobs <= 1000:
                    parser.error('max-jobs must be 1..1000')
                items = []
                while len(items) < args.max_jobs:
                    item = corpus.run_one()
                    if item is None:
                        if args.watch:
                            time.sleep(1)
                            continue
                        break
                    items.append({'id': item['id'], 'state': item['state']})
                result = {'processed': len(items), 'items': items}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result.get('ok') is False else 0
    except (CorpusError, json.JSONDecodeError) as error:
        print(json.dumps({'ok': False, 'error': {'code': getattr(error, 'code', 'INVALID_JSON'), 'message': str(error)}}))
        return 1
    finally:
        if corpus:
            corpus.close()


if __name__ == '__main__':
    raise SystemExit(main())
