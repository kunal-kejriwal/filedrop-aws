"""Core stack: S3, DynamoDB, SNS, SQS + DLQs, and the three back-of-house Lambdas.

Layout of resource groups (top-to-bottom in this stack):
    1. Storage         — S3 bucket + two DynamoDB tables
    2. Messaging       — SNS topic + notify/audit SQS queues (each with DLQ) + alarms topic
    3. Compute         — process/notify/audit Lambdas (Python 3.12, powertools, X-Ray)
    4. Wiring          — EventBridge rule, SNS→SQS subscriptions with filter, alarms
    5. IAM             — least-privilege grants; every grant carries a code comment on why
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_s3 as s3,
    aws_ses as ses,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_sqs as sqs,
)
from constructs import Construct

from infra.bundling import lambda_asset


class FiledropCoreStack(cdk.Stack):
    """All non-API resources. Exposes the uploads bucket and table for the API stack."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        sender_email: str,
        alarm_email: str | None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------- storage
        # Portfolio project: DESTROY on stack teardown so we don't leave charges behind.
        # In production this would be RETAIN + point-in-time recovery.
        self.uploads_bucket = s3.Bucket(
            self,
            "UploadsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # EventBridge notifications are OFF by default on new buckets — this flag
            # turns them on so we can filter with EventBridge rules instead of the
            # legacy S3 notification config (which only supports one target per event).
            event_bridge_enabled=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-uploads-after-7-days",
                    prefix="uploads/",
                    expiration=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Main uploads table — TTL cleans up abandoned AWAITING_UPLOAD rows after 48h.
        # Portfolio: PITR off (default) + DESTROY on teardown. Prod would flip both.
        self.uploads_table = ddb.Table(
            self,
            "UploadsTable",
            partition_key=ddb.Attribute(name="upload_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Audit table — every processed event lands here. Composite key so a single
        # upload can accrue multiple event records (uploaded, rejected, notified, ...).
        self.audit_table = ddb.Table(
            self,
            "AuditTable",
            partition_key=ddb.Attribute(name="upload_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="event_timestamp", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Email-index table — anti-abuse gate for the public demo.
        # request_upload does a conditional PutItem here before creating a slot;
        # a duplicate within the TTL window is rejected with HTTP 429. TTL clears
        # the row after 24h so genuine users can retry.
        self.email_index_table = ddb.Table(
            self,
            "EmailIndexTable",
            partition_key=ddb.Attribute(name="email", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------------------------------------------------------------- messaging
        events_topic = sns.Topic(self, "EventsTopic", topic_name="filedrop-events")

        # Alarms topic — one email endpoint if provided, otherwise created empty and
        # subscribed to later (still usable via SNS console).
        alarms_topic = sns.Topic(self, "AlarmsTopic", topic_name="filedrop-alarms")
        if alarm_email:
            alarms_topic.add_subscription(sns_subs.EmailSubscription(alarm_email))

        # DLQs first (each queue references its own DLQ before its main queue is created).
        notify_dlq = sqs.Queue(
            self,
            "NotifyDlq",
            queue_name="filedrop-notify-dlq",
            retention_period=Duration.days(14),
        )
        audit_dlq = sqs.Queue(
            self,
            "AuditDlq",
            queue_name="filedrop-audit-dlq",
            retention_period=Duration.days(14),
        )
        # Separate DLQ for EventBridge → process Lambda target failures.
        eb_dlq = sqs.Queue(
            self,
            "EventBridgeDlq",
            queue_name="filedrop-eventbridge-dlq",
            retention_period=Duration.days(14),
        )

        notify_queue = sqs.Queue(
            self,
            "NotifyQueue",
            queue_name="filedrop-notify-queue",
            # Give the notify Lambda time to render + call SES before redelivery.
            visibility_timeout=Duration.seconds(60),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=notify_dlq),
        )
        audit_queue = sqs.Queue(
            self,
            "AuditQueue",
            queue_name="filedrop-audit-queue",
            visibility_timeout=Duration.seconds(30),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=audit_dlq),
        )

        # SNS → SQS subscriptions. notify-queue filters to file_uploaded events only;
        # audit-queue is the catch-all so audit records exist for rejections too.
        events_topic.add_subscription(
            sns_subs.SqsSubscription(
                notify_queue,
                raw_message_delivery=True,
                filter_policy={
                    "event_type": sns.SubscriptionFilter.string_filter(allowlist=["file_uploaded"]),
                },
            )
        )
        events_topic.add_subscription(
            sns_subs.SqsSubscription(audit_queue, raw_message_delivery=True)
        )

        # ---------------------------------------------------------------- ses identity
        # The sender identity is verified out-of-band in the SES console (or via
        # the SES CreateEmailIdentity API + click-through on the confirmation email).
        # We import the existing identity here so `cdk destroy` never touches it —
        # the identity outlives any single deploy of this stack.
        sender_identity = ses.EmailIdentity.from_email_identity_name(
            self,
            "SenderIdentity",
            email_identity_name=sender_email,
        )

        # ---------------------------------------------------------------- compute
        common_env = {
            "POWERTOOLS_SERVICE_NAME": "filedrop",
            "POWERTOOLS_LOG_LEVEL": "INFO",
            "UPLOADS_TABLE": self.uploads_table.table_name,
            "AUDIT_TABLE": self.audit_table.table_name,
            "UPLOADS_BUCKET": self.uploads_bucket.bucket_name,
            "EVENTS_TOPIC_ARN": events_topic.topic_arn,
            "SENDER_EMAIL": sender_email,
        }

        process_fn = lambda_.Function(
            self,
            "ProcessFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_asset("process"),
            timeout=Duration.seconds(30),
            memory_size=256,
            tracing=lambda_.Tracing.ACTIVE,
            environment=common_env,
        )

        notify_fn = lambda_.Function(
            self,
            "NotifyFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_asset("notify"),
            timeout=Duration.seconds(30),
            memory_size=256,
            tracing=lambda_.Tracing.ACTIVE,
            environment=common_env,
        )

        audit_fn = lambda_.Function(
            self,
            "AuditFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_asset("audit"),
            timeout=Duration.seconds(15),
            memory_size=192,
            tracing=lambda_.Tracing.ACTIVE,
            environment=common_env,
        )

        # ---------------------------------------------------------------- iam grants
        # process: HEAD/tag the uploaded object, flip uploads-table status, publish SNS.
        # Scoped to uploads/* only so the function can never touch other prefixes.
        self.uploads_bucket.grant_read(process_fn, objects_key_pattern="uploads/*")
        process_fn.add_to_role_policy(
            iam.PolicyStatement(
                # PutObjectTagging is a distinct action from Put/GetObject; we grant it
                # narrowly rather than using grant_put on the whole bucket.
                actions=["s3:PutObjectTagging"],
                resources=[self.uploads_bucket.arn_for_objects("uploads/*")],
            )
        )
        self.uploads_table.grant_write_data(process_fn)  # conditional UpdateItem
        events_topic.grant_publish(process_fn)  # publish SNS event

        # notify: read uploads row, sign GET URL, send SES email, mark NOTIFIED.
        self.uploads_bucket.grant_read(notify_fn, objects_key_pattern="uploads/*")
        self.uploads_table.grant_read_write_data(notify_fn)
        notify_fn.add_to_role_policy(
            iam.PolicyStatement(
                # SES SendEmail is authorised against *both* the identity being sent
                # from and the SES configuration set the account/identity is routing
                # through. Many AWS accounts have a default configuration set enabled
                # (e.g. `my-first-configuration-set`), so scoping resources to only
                # the identity ARN causes AccessDenied at SendEmail time.
                # We grant on the identity + any configuration-set in the account;
                # the effective send restriction is still that the sender identity
                # must be verified in SES.
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    sender_identity.email_identity_arn,
                    f"arn:aws:ses:{self.region}:{self.account}:configuration-set/*",
                ],
            )
        )

        # audit: append-only writes to the audit table. No read needed.
        self.audit_table.grant_write_data(audit_fn)

        # SQS → Lambda triggers. batch_size=10 balances throughput and blast radius:
        # one poison message can fail a whole batch, so keep batches small.
        notify_fn.add_event_source(lambda_events.SqsEventSource(notify_queue, batch_size=10))
        audit_fn.add_event_source(lambda_events.SqsEventSource(audit_queue, batch_size=10))

        # ---------------------------------------------------------------- eventbridge
        # Match S3 Object Created events for our bucket + uploads/ prefix only.
        events.Rule(
            self,
            "UploadObjectCreatedRule",
            rule_name="filedrop-object-created",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [self.uploads_bucket.bucket_name]},
                    "object": {"key": [{"prefix": "uploads/"}]},
                },
            ),
            targets=[
                targets.LambdaFunction(
                    process_fn,
                    # If EventBridge cannot deliver (throttling, bad code), messages
                    # land here for redrive rather than being silently dropped.
                    dead_letter_queue=eb_dlq,
                    retry_attempts=2,
                )
            ],
        )

        # ---------------------------------------------------------------- alarms
        for name, dlq in (
            ("NotifyDlqDepth", notify_dlq),
            ("AuditDlqDepth", audit_dlq),
            ("EventBridgeDlqDepth", eb_dlq),
        ):
            alarm = cw.Alarm(
                self,
                f"{name}Alarm",
                metric=dlq.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(1)
                ),
                threshold=0,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                alarm_description=f"{dlq.queue_name} depth > 0 — investigate.",
            )
            alarm.add_alarm_action(cw_actions.SnsAction(alarms_topic))

        # ---------------------------------------------------------------- outputs
        cdk.CfnOutput(self, "UploadsBucketName", value=self.uploads_bucket.bucket_name)
        cdk.CfnOutput(self, "UploadsTableName", value=self.uploads_table.table_name)
        cdk.CfnOutput(self, "EmailIndexTableName", value=self.email_index_table.table_name)
        cdk.CfnOutput(self, "AuditTableName", value=self.audit_table.table_name)
        cdk.CfnOutput(self, "EventsTopicArn", value=events_topic.topic_arn)
        cdk.CfnOutput(self, "AlarmsTopicArn", value=alarms_topic.topic_arn)
