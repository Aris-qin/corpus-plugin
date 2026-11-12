# Operator-only embedding backfill

`embed-backfill` is a maintenance CLI, not a seventh model tool. Use explicit
settings and an authorized containment root. First make a consistent SQLite
backup and point a **copy** of settings at that backup; do not reuse production
settings for a smoke test.

```sh
python -m corpus_core.cli --sandbox-root /ABSOLUTE/candidate \
  --settings /ABSOLUTE/candidate/settings-copy.json embed-backfill
python -m corpus_core.cli --sandbox-root /ABSOLUTE/candidate \
  --settings /ABSOLUTE/candidate/settings-copy.json embed-backfill \
  --limit 30 --batch-size 10 --max-calls 3 --max-chars 32000 \
  --sleep 0.2 --apply --report /ABSOLUTE/candidate/new-report.json
```

The default is dry-run: statistics only, with no provider calls or profile/batch
registration. Opening the corpus uses its normal initialization, so use a copy
even for dry-run if absolutely no database writes are permitted.

`--limit 0` selects all missing chunks. `--max-calls` and `--max-chars` default
to the embedding policy's small ingestion budgets; explicit overrides are
**per invocation** operator authorizations, not lifetime budgets. `--max-chars`
is both the total Unicode character ceiling and a batch ceiling. Text containing
NUL is counted in full, unlike SQLite `length(text)`. No monetary estimate is
invented: the CLI does not enforce the ingestion policy's per-call USD estimate;
operators must choose and approve their own backfill budgets before applying.

Chunks are selected in stable ID order for the configured profile only. Existing
legacy vectors are never claimed. ASCII costs approximately 1/4 token and
non-ASCII approximately 2/3 token; chunks estimated above 8,000 tokens are skipped,
not truncated. The JSON report includes the count and first 20 IDs. A chunk
larger than the remaining character budget stops processing without a request;
increase the budget explicitly on a later invocation.

For DashScope `text-embedding-v4`, batch sizes above 10 are proactively reduced
to 10 and the requested/effective sizes and downgrade flag are reported. This
avoids repeated rejected requests; no speculative oversized paid probe is made.

Each request commits `started` and its chunk mapping before HTTP. Successful
vectors, `INSERT OR IGNORE` chunk records and `succeeded` commit together. DNS or
other proven pre-send failures become `not_sent` and may be resumed. Other
exceptions become `ambiguous`; a hard process interruption may leave `started`.
HTTP 429 is separately reported as `PROVIDER_RATE_LIMITED` and becomes `not_sent`,
with no vectors published. The same deterministic batch ID is retried up to three
additional times after 2, 8 and 20 seconds. Override with `--rate-limit-retries`
(integer 0–3) and `--rate-limit-delays SECONDS SECONDS SECONDS` (each finite, 0–60).
Every attempt counts against both invocation call and character budgets; retries
never exceed either budget. Exhausted retries or insufficient retry budget stop
with `ok=true`, `stopped_reason=rate_limited`, and unchanged pending vectors for
that batch. No profile lock or manual reconciliation is needed; rerun directly.
HTTP 500/502/503/504 remain billing-ambiguous and are never retried automatically;
401/403 remain non-retryable `PROVIDER_FAILED` and conservatively ambiguous.
Both `started` and `ambiguous` block all automatic requests for that profile, including when
budgets or batch sizes change. Inspect `embedding-recovery-inventory` and use the
existing local `reconcile-embedding --manifest PATH` with recovered provider
vectors and evidence. On a later apply, reconciled successful backfill batches
publish their recovered vectors without another provider call. Uncertain billing
does not authorize a retry. Concurrent progress safely stops with a rerun hint.

Reports must be new bounded paths and never overwrite existing files. Console
and report summaries contain neither vectors nor credentials. Vector data is
stored only in the existing database vector/batch columns, as required for
indexing and recovery. Sensitive model tools remain disabled.
