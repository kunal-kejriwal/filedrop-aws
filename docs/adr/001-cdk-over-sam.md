# ADR 001 — CDK over SAM for infrastructure

## Context

Filedrop is a small AWS project (six discrete services, no VPC, no ECS/EKS) but every resource carries wiring: SNS-to-SQS with filter policies, EventBridge rule pattern, cross-Lambda IAM grants, DLQs with `maxReceiveCount` on each queue, alarms on each DLQ. The choice was between AWS SAM (CloudFormation with template-level shortcuts for serverless resources) and AWS CDK v2 with the Python bindings.

SAM's strengths: fewer moving parts, YAML that CloudFormation-literate readers can review directly, `sam local invoke` for local iteration. Its weaknesses show up as scale of wiring grows: filter policies are inline JSON, cross-resource ARNs are `!GetAtt` strings scattered across templates, and IAM policies drift from the resource they protect.

CDK trades a bigger runtime dependency (Node CLI + Python bindings) for constructs that collapse wiring into one-liners. `topic.add_subscription(SqsSubscription(queue, filter_policy=...))` becomes one call that generates both the subscription resource and the subscribe permission. `queue.grant_consume_messages(fn)` colocates the IAM policy with the resource it protects.

## Decision

Use CDK v2 with the Python bindings. Two stacks (`FiledropCoreStack`, `FiledropApiStack`) with cross-stack references for the uploads bucket and table.

Reasons that tipped the balance:

1. **Python everywhere.** Handlers, tests, scripts, and infra all live in one language. A reader following an IAM grant from `core_stack.py` into `functions/process/handler.py` doesn't switch mental models.
2. **Construct-level testability.** CDK produces CloudFormation via `synth`, but you can also snapshot-test constructs and assert IAM statements. That's a story worth telling for a portfolio piece.
3. **Idiomatic filter policies.** `sns.SubscriptionFilter.string_filter(allowlist=["file_uploaded"])` is self-documenting; the SAM equivalent is a JSON blob under `FilterPolicy:` in the subscription resource.
4. **Signal for AWS-facing employers.** CDK is the current AWS-recommended IaC for greenfield projects; SAM is maintenance mode for many teams.

## Consequences

- Deploys require both Python and Node (`aws-cdk@2` CLI). The Makefile and deploy workflow handle this, but a first-time contributor has more prereqs than a pure SAM project would demand.
- `cdk synth` output is verbose CloudFormation — code review of infra changes means reading the CDK diff, not the CloudFormation. Anyone new to CDK has to trust the construct library.
- Bundling Lambdas via `lambda.Code.from_asset(..., bundling=...)` requires Docker in local dev, which is heavier than SAM's zip-based packaging. Acceptable trade for keeping powertools versioned in `pyproject.toml`.
- If the project ever needs to hand off to a team unfamiliar with CDK, the migration cost is real — CloudFormation import is possible but painful. Documented so future-me knows this is a one-way door.
