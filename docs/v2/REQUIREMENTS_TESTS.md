# Requirements → safe tests

Run only `tests/safe` with explicit `--sandbox-root` and contained `--basetemp`,
HOME/TMP/cache/bytecode paths. `conftest.py` rejects missing isolation and nonlocal
candidate imports; ordinary tests deny socket connections. All data is SYNTHETIC.
The supplied actual legacy DDL is used without opening the production database.

| Requirement | Safe tests | Evidence/limits |
| --- | --- | --- |
| Six tools; strict action/nested contracts | test_contract_matrix, test_action_roundtrip, test_tools | Actual offline business invocation; SDK recorder is not real host acceptance |
| Hash-addressed custody, no-clobber, rollback/idempotency | test_storage, test_regressions, test_failure_paths | Originals retained after downstream failure; fsck never deletes |
| Every consumption hash+size, corrupt retry/race | test_parent_corrections | Correct-behavior RED/GREEN; old active generation retained |
| Blob equality versus kind/coverage/media semantics; dedup revision/lifecycle | test_parent_corrections, test_contract_matrix | Same bytes can create explicit formal version; stale/recycled writes reject |
| Merge canonical discovery, old/target contexts, stable URI/navigation | test_parent_corrections | Both sources discoverable; aliases/chains/loops refused |
| Frozen actual known merge impact | test_parent_corrections | Later relation changes reject approval; unknown external refs not enumerable |
| Known retraction/correction/lifecycle status and reviews | test_parent_corrections | Search/fixed version/chunk expose status/provenance/warnings/scope/date/remaining |
| Publication freezes work/research revisions/status | test_parent_corrections, test_research | Stale before/after rejection; legitimate superseded formal citations allowed |
| Strict metadata/DOI normalization/conflict/index consistency | test_parent_corrections, test_safety | Malformed nested fields reject; DOI ownership updated atomically |
| Batch contracts are not alternate writes | test_parent_corrections, test_contract_matrix | Same top-level schema/bounds reused for nested draft/source |
| Historical formal versus draft; resolved reviews | test_parent_corrections | superseded_formal not draft; current navigation finds fixed review |
| Global/linked/Chinese/formal-priority/filter-before-budget | test_retrieval, test_parent_corrections, test_bounded_integrations | Exact bounded scan; truncated explicit; older fixed sources accessible |
| Related current/draft reports and explicit research body lookup | test_action_roundtrip, test_parent_corrections | Citation-based navigation, not substitution for original evidence |
| Fixed chunk neighbor expansion | test_bounded_integrations | Same generation, bounded radius; no verified same-level tree semantics |
| Durable leases/cancel/retry/process kill/atomic activation | test_crash_recovery, test_regressions, test_failure_paths | Real child kill; fsync/disk-full and provider faults injected, not actual filled disk |
| Text/Markdown/HTML bounds/locators | test_safety, test_parser_budget | 10,001 chunks fail closed; no fabricated pages |
| Functioning isolated pypdf + real page/hash provenance | test_bounded_integrations | Real subprocess/two-page synthetic PDF; no OCR/formula/figure/bbox extraction |
| SSRF DNS/redirect/type/size/pinning | test_safety, test_failure_paths, test_bounded_integrations | Offline adapter responses only, no live external acquisition |
| URL submit + validated deterministic DOI resolver | test_bounded_integrations | Provenance stored; invalid ID/login fail; coverage unknown |
| Configured submit/runner/query profile | test_bounded_integrations | Real API code, explicit offline transport fixture; production mock/injected transport refused |
| Batch checkpoint/retry skipping/ambiguous fee stop | test_bounded_integrations | Successful calls not repeated after activation failure; lexical fallback and incomplete step |
| Scope/cost reservations; explicit safe-partial budget recovery | test_bounded_integrations, test_predeploy_repair | Queued/running jobs cannot expand; operator retry binds revised budgets, never model/endpoint identity; billing unverified |
| Model fingerprint/vector dimension/no network in write TX | test_regressions, test_failure_paths, test_bounded_integrations | Different models not mixed; legacy_unknown never queried |
| Read-only WAL snapshot/raw field+BLOB/count/hash verification | test_migration, test_regressions | Synthetic actual DDL; worker never reads real DB |
| Trusted sqlite_vec/actual vec0 DDL/shadow state/logical unknown vectors | test_bounded_integrations | Real extension and synthetic vector; backup/raw tables retained |
| Stable project labels/status/normalized relationships/unresolved | test_regressions, test_bounded_integrations | Raw relationships plus normalized mappings; unresolved issues retained |
| Changed snapshot refused without duplicate lineage | test_parent_corrections | Identical replay idempotent; changed snapshots require fresh candidate |
| Unknown virtual modules/future DDL rejected with backup | test_bounded_integrations, test_migration | No silently accepted unknown future columns/tables/modules |
| Portable root/no production fallback/local imports | conftest, test_bounded_integrations, test_contract_matrix, tools/validate.sh | Explicit authorization; launcher/kernel, not same-shell identity security |
| Reviews/guidance/reminders/metadata not research body | test_research, test_action_roundtrip | Structural fields and repeated reminders; full ontology/age policy incomplete |
| Export outbox/drift, wiki/fact link-only | test_tools, test_failure_paths, test_action_roundtrip | No body rewriting or Git automation |
| Real owned-content purge, dependency barriers, tombstones, containment, crash/retry/concurrency | test_purge_boundary | Synthetic fixtures; no secure erasure, legacy backup deletion or human identity proof |
| Parent-only bounded embedding + native rerank smoke adapter path | test_live_smoke | Offline synthetic adapter tests only; worker never executes live utility |
| Full safe suites + compile from source/sdist/wheel | tools/validate.sh | Exact commands/counts/exit codes/hashes in fresh ../evidence/validation-* |
| Real SDK/human WebUI/paid API/production snapshot/deploy | none | Explicitly unproven, unauthorized and not performed |

2026-11-11 repairs: `test_predeploy_repair` covers fresh isolated directory entrypoint
imports (not Hermes discovery), partial/zero/ambiguous vector jobs, recovered-response
reconciliation, durable parse/batch reuse, concurrent retries, active generation
preservation, historical-state correction and default-disabled tool/CLI/Python paths.
`test_predeploy_adoption` covers deterministic original-copy adoption, mismatch/missing/
escape/failure rollback, idempotence, exact verified vector subsets, unknown/mock/orphan
barriers and actionable 15-item quarantine/hierarchy inventory. The fixture
`simulated_approval_boundary` is an explicit unit-test monkeypatch for historical
approved-path protocols; it is not a product approval mode or real human integration.

## RED/GREEN interpretation

Preserved parent-corrections RED: 15 failures. Bounded integration RED: 7 failures,
1 already-passing isolation guard. Policy/future-DDL hardening RED: 2 failures.
Newly materialized merge refs and merged-original status filtering RED: 2 failures.
Assertions demand corrected behavior, not continued reproduction of bugs. Original
independent review/evidence and all intermediate failed GREEN attempts remain.
Additional tests against already-correct behavior are verification, not relabeled
RED-first work. Passing synthetic tests never establishes production integration
or full M0–M6 acceptance; see `ACCEPTANCE.zh-CN.md` for exact remaining scope.
