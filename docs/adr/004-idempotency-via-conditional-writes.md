# ADR 004 — Idempotency via DynamoDB conditional writes

## Context

Both S3 and EventBridge deliver `Object Created` events *at least once*. Duplicate deliveries are rare but they happen — throttling retries, control-plane hiccups, deploys that race with in-flight events. The same is true of SNS → SQS: the notify Lambda can see the same message twice if the first invocation succeeded but SQS didn't get the delete confirmation.

Options considered:

1. **A dedicated dedup table.** Every event handler writes `(event_id, seen_at)` with a conditional put; duplicates fail the condition. Standard pattern but adds a table, a write per event, and TTL management.
2. **AWS Lambda Powertools `idempotency` decorator.** Powertools ships this — it wraps handlers and stores results in DynamoDB keyed by event hash. Powerful but heavy; the result-caching aspect is unused for our flow.
3. **Conditional writes on the existing status column.** Filedrop's state machine already has an ordered sequence: `AWAITING_UPLOAD → UPLOADED → NOTIFIED` (or `REJECTED`). Each transition can be a conditional `UpdateItem` that only fires when the current status is the expected predecessor. Duplicate events fail the condition and log-and-exit.

## Decision

Use option 3. Every status transition is a conditional `UpdateItem`:

- `process`: `SET status = UPLOADED IF status = AWAITING_UPLOAD`
- `notify`:  `SET status = NOTIFIED IF status = UPLOADED`

`ConditionalCheckFailedException` is caught explicitly, logged as `duplicate_event_suppressed` / `duplicate_notify_suppressed`, and the Lambda returns success. Any other DynamoDB error re-raises and lets SQS/EventBridge retry as normal.

Reasons:

1. **No extra table.** The dedup gate is a property of state we already store.
2. **State machine enforcement is free.** The same condition that dedups events also prevents impossible transitions (e.g., `NOTIFIED → UPLOADED`) from ever landing.
3. **Cheap.** One `UpdateItem` per event instead of one dedup `PutItem` + one status update.
4. **Debuggable.** Duplicate suppressions are visible as structured log events, correlatable by `upload_id`.

## Consequences

- This only dedups events that would advance the state machine. If a duplicate SNS message somehow arrived while the row was mid-transition, both invocations might succeed with different transition targets — but the state machine is linear, so at most one path succeeds.
- The `REJECTED` transition is *not* guarded by a condition: a rare double-reject would overwrite `rejection_reason` idempotently. Acceptable since REJECTED is terminal and there's nothing downstream to fan out from.
- The condition ties Lambdas to the shape of the status column. Adding a new state means updating the conditions in every handler. Documented in `shared/models.py` where the `UploadStatus` enum lives — that's the one place a change would radiate from.
- Trade-off vs Powertools' idempotency decorator: we lose result-caching (if a Lambda runs twice, the second run does its work up to the DDB update before finding the duplicate). For SES this means we could theoretically send two emails if two `notify` invocations race between the SES call and the DDB update. In practice SQS visibility timeout + `maxReceiveCount=3` bounds this; we accept the risk.
