# Corpus candidate — not deployed

**2026-11-11 predeploy correction:** see `docs/predeploy-repair-20261111.zh-CN.md`.
Sensitive actions are now **disabled**, including Python/CLI operator approval,
formal publication, batch apply, merge, permanent deletion and old purge recovery.
Only `safety_mode: "disabled"` is accepted; there is no enable flag or shipped human
approval adapter. Existing approved-path tests explicitly simulate the gate in tests,
not in product configuration. Drafts, search, read, recycle and restore remain usable.

Executable candidate of the approved **six-tool** corpus scope, not complete product
acceptance or authorization to deploy. See `docs/ACCEPTANCE.zh-CN.md` and
`docs/REQUIREMENTS_TESTS.md`. No production defaults, legacy handlers/tests,
credentials, system configuration or wiki/fact bodies are discovered or read.

Python 3.11+ and SQLite FTS5 are required. Lexical functionality uses stdlib.
Optional **pypdf** enables PDF extraction; optional **sqlite_vec** enables migration
of the reviewed vec0 schema. Use the provided local validation venv, not a global
install. No paid service or external URL was called during worker validation.

## Explicit isolation

Before construction, the launcher supplies an existing absolute authorized root
through `CORPUS_AUTHORIZED_SANDBOX_ROOT`, CLI `--sandbox-root`, or Python constructor
`sandbox_root=`. Missing roots, filesystem-root sandboxes and path/symlink escapes
fail closed. Package/build/test/validation code contains no development-path
constants. This is a containment contract, **not** unforgeable permission against
a same-shell user; actual isolation comes from the parent launcher/kernel.

With `SANDBOX` and `PYTHON` supplied by the authorized launcher:

```sh
export CORPUS_AUTHORIZED_SANDBOX_ROOT="$SANDBOX"
export HOME="$SANDBOX/home" TMPDIR="$SANDBOX/tmp"
mkdir -p "$HOME" "$TMPDIR"
export PYTHONDONTWRITEBYTECODE=1 CORPUS_OFFLINE=1
ROOT="$SANDBOX/tmp/manual-candidate"
"$PYTHON" -m corpus_core.cli --root "$ROOT" probe
"$PYTHON" -m corpus_core.cli --root "$ROOT" call corpus_ingest --json '{"action":"submit","operation_id":"synthetic-1","inputs":[{"source":"text","title":"SYNTHETIC example","content":"SYNTHETIC 中文研究 evidence"}]}'
"$PYTHON" -m corpus_core.cli --root "$ROOT" runner --max-jobs 10
"$PYTHON" -m corpus_core.cli --root "$ROOT" call corpus_search --json '{"mode":"evidence","query":"研究"}'
"$PYTHON" -m corpus_core.cli --root "$ROOT" fsck
bash tools/validate.sh --sandbox-root "$SANDBOX" --python "$PYTHON"
```

The validation script collects only `tests/safe`, checks local import origins,
runs full source/sdist/wheel suites and compileall, and records exact commands,
exit codes, schema, contracts and artifact hashes in a fresh evidence directory.
HOME/TMP/cache/fixtures/build outputs stay inside the authorized root. The offline
PEP 517 backend requires the same environment or `sandbox_root` config setting.

## Tools and integrity

SDK registration exposes exactly `corpus_ingest`, `corpus_search`, `corpus_read`,
`corpus_reference`, `corpus_manage`, `corpus_jobs`. Set `CORPUS_SANDBOX_ROOT` to the
dedicated data root, or `CORPUS_SETTINGS_FILE`, as well as the authorized root.
There is no automatic runner/research. The parent separately reports real SDK
six-tool registration/publication with a simulated operator, a six-tool model draft
lifecycle, and real-copy vec0 migration with original-payload comparison. Those are
parent checks, not this worker's offline validation. Real human approval, paid
embedding/rerank remain unverified by the worker. Native-source purge now has offline
deletion/recovery coverage; it is not secure erasure or verified human approval.
Contracts: `docs/ACTION_CONTRACTS.md`.

Schema v4 separates immutable blobs/versions/generations/chunks/research revisions
from navigation, contexts, approvals, jobs and outbox. Additive internal tables
hold raw legacy vectors/relationships and embedding checkpoints. Originals are
fsynced/no-clobber published before catalogue references. Every runner/retry checks
hash **and** size, including the bytes actually consumed by the parser. Failed
activation preserves the old active generation. Blob equality is not version-kind,
coverage or media equality; explicit work writes check revision/lifecycle on dedup.

Merge keeps old fixed URIs and exposes both sources under the canonical target in
global and all old/target contexts. Navigation/history show merged versions. Loops,
alias restoration and transitive merge chains are refused. Proposal-time actual
relation/ref impact is frozen; changed impact requires reproposal. Unknown external
references remain non-enumerable. Purge fails closed pending safe shared-blob GC.

Publication freezes revision hashes and the deduplicated recursive evidence closure
(maximum 32 edges deep, 1000 dependency nodes; cycles/limits fail closed), including
source/canonical-work status and relevant version/chunk/fixed-revision review
fingerprints. Apply recomputes this closure in the commit transaction. Stale approval
returns before/after differences; a new explicit proposal may accept known historical,
retracted or unresolved evidence. Research reads expose upstream status, warnings,
and unresolved reviews. Nested retention uses identical top-level contracts.
Metadata/assessments have strict fields/types; DOI correction updates normalized
identifier ownership atomically, accepts aliases of the same canonical work, and
rejects different-work conflicts without rewriting old citations/alias provenance.
Unchanged metadata fields retain their recorded dates. Research prose is not
metadata. The historical operator CLI bound proposal/hash/revision but did not prove
human identity; it now fails closed. Tools have no self-confirm boolean.
Export failure remains distinct from committed data. No Git/deploy/wiki-body path.

## Retrieval, parsing and acquisition

Search filters `work_ids`, `version_ids`, `version_kinds`, `kinds`, `coverage`,
`retraction_status`, `year_min`, `year_max` and normalized `doi` before the candidate
budget. Formal/preferred sources rank first; older fixed evidence remains readable
and filterable. RRF/component scores and truncation are explicit. Chinese substring
fallback is not a claimed Chinese segmenter. Source status, provenance, lifecycle,
retraction/correction warnings and scoped dated reviews appear in search and fixed
reads. Formal-use reads repeat remaining items. Historical published revisions are
`superseded_formal`, not drafts. Overview prioritizes formal records; evidence search
includes related current/draft report navigation from explicit citations. Separate
`mode=research` searches research bodies without substituting them for source evidence.
Chunk `before`/`after` expands only within the fixed generation. Similar uses the
configured semantic profile, else explicit lexical degradation; chunks are flat,
not a verified same-level hierarchy. Optional bounded reranking is described below;
it does not change chunk generation or source precedence.

Text/Markdown/visible HTML work offline. PDF uses the trusted current interpreter
with `-I -B`, cleared environment, network-denying worker, CPU/memory/output/FD bounds
and timeout. It inherits the parent kernel boundary, not an independent portable
OS sandbox. Synthetic two-page tests exercise actual pypdf and real page provenance;
original/canonical/locator hashes agree. Images/formulas/OCR/bboxes remain unextracted
with warnings; scanned-page text and fake page coordinates are not invented.

Explicit URL submission uses SSRF public HTTPS/443, public-only DNS, pinned peer/TLS,
bounded redirects/bytes/types and saved provenance. Identifier acquisition supports
only validated deterministic DOI resolver URLs, not PMID search, bibliographic
matching or automatic full-text discovery. Login/challenge/unsupported responses
fail honestly. Acquired coverage stays `unknown`. Real site compatibility is untested.

## Configured embedding path

CLI `--settings FILE` replaces `--root`; the plugin uses `CORPUS_SETTINGS_FILE`.
The bounded nonsecret JSON file must be inside the authorized root. Example shape
(real values require separate operator/provider authorization):

```json
{
  "data_root": "/ABSOLUTE_AUTHORIZED_SANDBOX/data",
  "embedding": {
    "profile": {"provider": "explicit-provider", "exact_model": "exact-model-id", "dim": 1024, "metric": "cosine"},
    "endpoint": "https://explicit-provider.example/embeddings",
    "key_env": "EXPLICIT_EMBEDDING_KEY",
    "policy": {"operation_ids": ["approved-submission"], "max_calls": 10, "max_chars": 10000, "batch_size": 10, "max_cost_usd": 0.10, "max_cost_per_call_usd": 0.01}
  }
}
```

`embedding: null` disables vector retrieval (lexical candidates can still be reranked).
Keys come only from the named
environment variable, never credential files. Production refuses mock profiles
and injected transports. Test fixtures do not prove a real service call.

Submit freezes profile/policy fingerprints; runner/query use the same explicit
provider/model/dimension/metric/preprocessing/provenance. The policy allows explicit
submission operation IDs, not automatic full-library reembedding. Calls/input bytes/
estimated cost ceilings are reserved durably before each batch. Successful batch
checkpoints survive activation failure; retries skip successful calls. Lost leases
and cancellation cannot begin another batch. Unknown/possibly billed failures or
crashed `started` batches stop automatic calls for that profile and expose lexical
fallback/incomplete vector job steps. No blind retry or automatic adjudication.

Configured but incomplete indexes finish as `partial`, not `succeeded`. Lexical/read
remain available with `index_incomplete`; completed parsing and provider batches are
cached. `corpus_jobs.retry` accepts safe partial work using explicitly revised operator
settings budgets for the original operation/profile/endpoint/key binding/batch size.
Ambiguous results block retry until controlled `reconcile-embedding` supplies the
recovered provider vectors (never authorization to blindly charge again). Discover
batch IDs and budget policy with CLI `embedding-recovery-inventory`.

## Optional rerank path

Existing settings without `rerank`, or with `rerank: null`, retain previous ranking.
Add the following top-level nonsecret object only with separate provider authorization:

```json
"rerank": {
  "protocol": "dashscope_native",
  "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank",
  "exact_model": "qwen3-rerank",
  "key_env": "EXPLICIT_RERANK_KEY",
  "timeout_seconds": 4,
  "candidate_budget": 30,
  "max_documents": 30,
  "max_document_chars": 10000,
  "max_calls": 10,
  "max_chars": 1000000
}
```

All fields are mandatory when enabled; unknown fields are rejected. The alternative
explicit protocol is `openai_compatible`, using the exact configured `/rerank`
endpoint with `model/query/documents/top_n` and `results` responses. DashScope uses
`input.query/documents`, `parameters.top_n/return_documents=false` and
`output.results`. Neither protocol, model, credentials nor host is guessed or
substituted. This example is configuration documentation, not live verification.

Hard limits: candidate/document counts 1..100; document characters 1..100000;
cumulative calls 1..10000; cumulative Unicode characters 1..10000000; positive socket
timeout at most 60 seconds; response at most 1 MiB. Timeout applies to network socket
operations; DNS uses the existing OS resolver, not a new overall deadline. Cumulative
calls and query-plus-document characters are reserved durably per exact config and
candidate database before attempting a request, even when the attempt later fails.
Changing config creates a separate budget identity, not a global account spending cap.
There is no currency-cost estimate for reranking and no automatic retry.

Only the bounded pre-ranked candidate prefix is sent, after scope/filter/lifecycle
checks. Formal/preferred precedence, locators, provenance, source warnings and the
existing five-chunks-per-version cap remain unchanged. Overlong documents are not
silently truncated: the entire rerank attempt degrades to deterministic pre-rerank
results. Unsubmitted candidates retain their original tail order with a candidate-limit
warning. Successful scored candidates expose `rerank_score` and `rerank_complete`;
failures expose `rerank_degraded_*`, never success. RRF/component scores are retained.

Ranking snapshots store scores/status only, not provider text, for 15 minutes; binding
includes settings, query, scope, filters and pre-rerank evidence. Sequential pages reuse
the snapshot without another rerank call. A concurrent/crashed in-flight reservation
fails closed to original ranking; a later changed snapshot makes its cursor stale rather
than mixing pages. Outages/missing keys are also cached for that window, so newly fixed
credentials do not immediately retry the same query. Existing embedding checkpoint
and ambiguous-call behavior is unchanged. The historical rerank-only acceptance is
recorded in `docs/rerank-repair-acceptance.zh-CN.md`; its purge blocker is superseded
by `docs/purge-repair-acceptance.zh-CN.md` for eligible native sources.

## Explicit permanent deletion

**Disabled in the current product pending trusted human approval integration.**
The following describes the historical business protocol exercised only by explicit
test-boundary simulation; it is not an available operator bypass.
Use the existing `corpus_manage` propose/apply actions with `maintenance: purge`,
the exact recycled work ID, expected revision and permanent-deletion reason. Inspect
the frozen inventory; the old operator-controlled CLI no longer issues approvals.
An `approval_ref` is not proof of human identity; `confirm=true` is not approval.
Known research/evidence/review/wiki/fact links, merge identities, shared blobs, active
jobs and legacy backup ambiguity block deletion. No references are silently removed.
Eligible purge removes all owned original bytes, canonical/chunk text, FTS and vectors,
including retired generations; non-content IDs remain as explicit purge tombstones.
The frozen proposal also discloses removal of ordinary organization/identifier mappings.

Deletion intent commits before any file is moved or unlinked. Failed cleanup returns
`state: cleanup_pending`, not success. Retry the identical apply request and operation
ID to resume deterministically, even after process restart or approval expiry. Pending
hashes cannot be ingested again. Tombstones cannot be restored or used as evidence.
No automatic background recovery or general orphan-file garbage collection is implied.
SQLite free pages/WAL, unmanaged copies, backups and filesystem snapshots are not
securely erased; migrated catalogues are conservatively blocked pending a separate
backup/history policy. See the purge acceptance report for the precise boundaries.

The parent-only opt-in `python -m corpus_core.live_smoke` utility exercises both
candidate provider runtimes with one short embedding and one native three-document
rerank request. It requires explicit bounded nonsecret settings, a fresh sandbox data
root and `--execute-paid-smoke`; keys are inherited environment variables only. The
worker does not execute it. Offline tests use explicitly synthetic transport responses.

Money ceilings depend on **operator-supplied conservative per-call upper prices**,
not verified provider billing. Pricing/token/invoice verification and paid service
authorization remain gates. Changed settings never enlarge running/queued jobs;
explicit retry of partial/failed work can bind a revised operator budget, but cannot
change profile/endpoint/key-variable/batch-size identity. Query calls share budgets and reuse identical
successful query checkpoints. The exact stdlib scan is bounded linear cost, not ANN.

## Read-only migration

CLI `legacy-inventory`, `adopt-original` and `claim-legacy-profile` expose review and
dry-run contracts without adding a seventh model tool. Exact snapshot/ID/hash/size
bindings are required. A filename or newly computed hash alone does not establish
historical original identity. Unknown vector profiles remain isolated unless trusted
integration code injects a per-vector provenance verifier; the CLI cannot invent one.
Bulk vector indexing refuses migrated chunks, and no legacy reembedding is authorized.

`migrate-readonly --source FILE` uses query-only/read-only SQLite backup API with WAL
consistency. Complete backup, raw schema/fields/BLOBs/shadow state and independent
counts/payload hashes are retained. The supplied actual DDL is tested with synthetic
**real vec0** rows using the trusted installed sqlite_vec package. Logical vectors
map to `legacy_unknown` and are never compared with new query embeddings. Stable
project slugs/labels/status and known relations map; unresolved relations report.
Unknown virtual modules/future tables/columns refuse with backup retained.
Identical snapshot replay is idempotent; any different snapshot after import is
refused instead of duplicating lineage. Use a fresh candidate for a changed snapshot.
Worker never opens production snapshots. Parent validates real migration separately;
raw file paths are claims, not proof of original custody.
