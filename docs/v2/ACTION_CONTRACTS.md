# Action contracts

Authoritative executable tagged unions: `corpus_core/contracts.py`.
Generated function schemas: each final `../evidence/validation-*/action-contracts.json`.
All unlisted top-level fields are forbidden. In particular `approved`, `confirm`,
replacement body in `apply`, arbitrary SQL/commands, budget changes on retry,
and project-owned original paths are not accepted. All strings are bounded;
IDs are strings, not booleans; pagination limits are integers 1–100, not numeric
strings or booleans. Source batches are 1–10 items, per input ≤10 MiB.

Every tool response is `{ok,request_id,result,warnings,error}`. On failure,
`error={code,message,retryable,details}` and result is null. Unexpected internals
are sanitized; DB_BUSY is retryable. Mutations require operation_id ≤200 chars;
same ID with identical canonical input returns a durable result after restart.
Different canonical input or tool domain returns IDEMPOTENCY_CONFLICT.

| Tool / tag | Action | Required besides tag | Optional |
|---|---|---|---|
| corpus_ingest / action | preflight | inputs | — |
| | submit | inputs, operation_id | — |
| corpus_search / mode | overview | context | limit, cursor |
| | evidence | query | scope(global/linked), context, top_k, cursor, filters, candidate_budget(1..10000) |
| | similar | ref (chunk URI) | top_k, cursor |
| | research | query | context, limit, cursor |
| corpus_read / view | item | ref | use_context |
| | version | ref (version URI) | use_context |
| | chunk | ref (chunk URI) | use_context, before(0..5), after(0..5) |
| | history | ref (work/reference URI) | limit, cursor |
| | resolve | ref (stable URI or legacy ID) | use_context |
| corpus_reference / action | save_draft | kind, body, evidence, structured, change_summary, reason, operation_id | record_id, parent_id, expected_revision |
| | propose | revision_id, operation_id | — |
| | propose_batch | items, operation_id | — |
| | apply | proposal_id, approval_ref, operation_id | — |
| | reject | proposal_id, operation_id | — |
| corpus_manage / action | metadata | target, expected_revision, changes, source_ref, operation_id | — |
| | link | target (work/reference ID), context, operation_id | — |
| | external_link | target (fixed URI), uri, basis, operation_id | — |
| | assess | target, expected_revision, assessment, source_ref, operation_id | — |
| | review | target (fixed URI), scope, source_ref, conclusion, remaining, operation_id | — |
| | propose | maintenance(merge/purge), target, expected_revision, reason, operation_id | merge_into, merge_expected_revision (required only for merge; forbidden for purge) |
| | apply | proposal_id, approval_ref, operation_id | — |
| | recycle / restore | target, expected_revision, operation_id | — |
| corpus_jobs / action | status | job_id | — |
| | list | — | state, limit, cursor |
| | retry / cancel | job_id, operation_id | — |

use_context is browse/report/submission. Evidence scope defaults global; merely
passing context does not restrict it. linked requires context. Cursor is HMAC-bound
to query, filter, profile, limit and result snapshot, with 15-minute expiry.
Dataset/filter drift rejects the cursor; callers restart pagination. Results have
no duplicate URI across unchanged pages. Candidate truncation and the five-chunk
per-version cap mean this is not a claim of exhaustive full-library recall.

## Nested contracts and semantics

- Input requires source(text/file/url/identifier) and title. Optional content,
  path, url, identifier are mutually constrained by source. text requires content;
  file requires a real path inside the explicit data root. URL uses the SSRF
  adapter; identifier accepts only a valid DOI for its deterministic HTTPS resolver.
  Both bound types/redirects/bytes and retain provenance; acquired coverage is
  unknown, never an automatic claim of full text. Login/challenge responses fail.
  Optional media_type, coverage, kind, contexts, version_kind, work_id,
  expected_revision, relationship_source, identifiers are strictly named.
  Existing-work versions require expected_revision plus fixed relationship_source.
  Identifier items require exactly scheme/ raw_value/ verification_source;
  schemes DOI/PMID/PMCID/URL are conservatively validated, originals retained,
  conflicting ownership rejected for review rather than silently merged.
- Research kind is topic_report/journal_profile/author_guidance/project_overview/
  submission_assessment/method_comparison. Evidence is a bounded list of fixed
  version/chunk/research-revision URIs; work navigation is not evidence.
  Existing records require matching formal expected_revision. Parent must belong
  to the same record. Bodies/revisions are immutable and include all frozen fields
  in the payload hash, not just prose.
- Author guidance requires structured article_type, journal_id and requirements
  for length/abstract/sections/references/figures/supplements/declarations/
  submission_materials/templates. Every requirement has exactly status, source_ref,
  locator, checked_at, value. not_provided requires null value. All sources must
  be in evidence. Journal profiles separate facts and assessments with source,
  locator, date and uncertainty. This is structural validation, not source fact
  verification or a complete journal-specific requirements ontology.
- Retention items are exactly `{action:save_source,input}`, `{action:save_draft,draft}`
  or `{action:publish_reference,revision_id}`. Source items must freeze inline text;
  draft has the save_draft fields. A proposal freezes all of them before approval.
  Per-item state is applied / failed / approved_pending_dependencies. Repeating a
  partial operation returns the same receipt; use a new operation ID to retry
  unfinished approved items. No expanded scope/cost is authorized by retry.
- Metadata changes only bounded title, list-of-string authors, integer year
  (1000..9999), valid DOI, and enumerated retraction_status (unknown/not_retracted/
  retracted/corrected/expression_of_concern), with source_ref. DOI correction updates
  normalized identity ownership atomically and rejects conflicts. Assessments only
  finite score(0..100) and bounded formula/uncertainty/basis strings. Neither edits research prose.
  Metadata provenance is recorded; a nonempty source_ref is not proof of reliability.
- External links are relative `wiki:...` or `fact:...` URIs, not wiki/fact bodies.
  Link coverage is explicitly limited to registered links. There is no wiki scan,
  rewrite, compile or fact task management action.
- Merge freezes both work revisions and actual known relation/ref impact fingerprints.
  Changed impact rejects with reproposal; aliases/chains/loops are refused. Both
  sources remain discoverable under canonical work/all original and target contexts.
  Apply keeps
  fixed source IDs and bytes. Purge is a separate explicit permanent-deletion
  proposal for an exact recycled work/revision, freezing owned blob identities,
  all generations and dependency state. Known citations (including chunk-only,
  report/evidence/provenance/review links), aliases, shared blobs, active jobs and
  ambiguous legacy backups block purge. Apply rechecks under the writer transaction.
  Eligible purge commits a non-content deletion journal and tombstones before
  staged file cleanup. `state=cleanup_pending` with `cleanup_error` is incomplete;
  repeat the identical apply/operation_id to recover. `state=purged` means managed
  application content cleanup completed, not secure erasure. All old known evidence
  blocks deletion; unregistered old URIs resolve to explicit non-evidence tombstones.
  No action silently deletes references or surviving shared bytes. Ordinary context
  and owned external-identifier mappings are disclosed in the frozen impact and removed.
  Imported catalogues with immutable backup/history uncertainty are blocked, not
  described as successfully purged. Separate operator approval remains not identity proof.
- retry is restricted to failed/cancelled jobs and preserves the stored budget;
  cancel is durable and prevents a worker from activating after cancellation.

Evidence filters combine work_ids/version_ids/version_kinds/kinds/coverage/
retraction_status lists and year_min/year_max/normalized doi before candidate bounds.
Formal/preferred sources rank first; old fixed versions remain accessible. Search
and fixed reads expose source status/warnings/reviews. Related report navigation is
explicit-citation-based; research mode separately searches research bodies.
Historical formal revisions are superseded_formal, never draft. Chunk expansion
stays in the fixed generation. Similar is semantic only with configured vectors,
otherwise explicitly degraded; flat chunks do not imply same-level tree semantics.

The six-tool count is unchanged. propose_batch/external_link and research-body
lookup keep approved retention, link-only collaboration and explicit body search
separate from original evidence. No operator-approve tool or self boolean exists.
# Current Predeploy Safety Override (2026-11-11)

The six tool names and strict action schemas remain unchanged. `corpus_jobs.list`
also accepts `state: partial`; safe partial retries bind explicit operator settings
budgets, preserve completed parsing/vectors and refuse ambiguous billed results.
`corpus_reference.apply` and `corpus_manage.apply` now return
`SENSITIVE_ACTION_DISABLED` before mutation, including historical approvals/receipts.
Python/CLI operator approval, formal exports and purge recovery also fail closed.
Only `safety_mode: disabled` is supported. Historical success-path examples below
are simulated business-protocol tests, not trusted human approval or an enable mode.
See `predeploy-repair-20261111.zh-CN.md` for recovery and legacy adoption contracts.
