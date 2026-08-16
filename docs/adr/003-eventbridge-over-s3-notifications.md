# ADR 003 — EventBridge over direct S3 → Lambda / S3 → SNS notifications

## Context

S3 emits `Object Created` events three ways:

1. **Direct to a Lambda function** via bucket notification.
2. **Via SNS topic**, which then fans out.
3. **Via EventBridge** (`event_bridge_enabled=True` on the bucket), which then routes via a rule.

For a single-consumer pipeline any of these works. Filedrop's requirements pushed harder:

- The `process` Lambda needs to fire on new objects under `uploads/` only.
- Test files, temp files, or objects under other prefixes must not invoke `process`.
- We want the option to add more consumers later (replay-to-a-test-env, dev observer) without changing the producer.

## Decision

Enable EventBridge notifications on the uploads bucket and match `Object Created` events with `detail.object.key = { "prefix": "uploads/" }` via a rule that targets the `process` Lambda.

Reasons:

1. **Filtering at the routing layer.** EventBridge event patterns support prefix/suffix matching on nested fields. Direct S3 notifications support a prefix filter but only *one* target per event type, and stacking multiple prefix rules gets awkward.
2. **Multi-target.** Adding a second target later (e.g., a debug-log Lambda) is one construct call, not a rewrite of bucket notification config that would fight with CDK for control.
3. **Archive + replay.** EventBridge event buses can be archived and replayed. Not used today, but the option is a free consequence of the choice.
4. **Decoupling.** The bucket doesn't know or care what consumers exist. Changing consumers doesn't touch the storage stack.
5. **DLQ on target failures.** EventBridge rule targets accept a DLQ (`filedrop-eventbridge-dlq`) for when the Lambda can't be invoked (throttling, IAM change mid-deploy). S3 direct notifications silently drop these.

## Consequences

- One extra hop of latency compared to S3 → Lambda direct. In practice this is single-digit milliseconds and well below SES + presign latencies — irrelevant for our SLA.
- EventBridge charges per event (~$1 per million). Trivial at portfolio scale but worth noting for cost-conscious readers.
- The event schema is EventBridge's (`detail.bucket.name`, `detail.object.key`), not the S3 direct-notification schema. The `process` handler parses accordingly; if we ever pointed it at direct notifications we'd have to adapt the parser.
- Rule targets can be misconfigured silently — the wrong ARN produces no invocations without a loud failure. Mitigated by the CDK cross-stack reference (`process_fn` is a construct, not a string), the DLQ, and the CloudWatch alarm on that DLQ.
