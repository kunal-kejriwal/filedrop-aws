"""Shared fixtures — moto-backed AWS clients + env for the Lambda handlers.

Every fixture that needs moto lives here so tests don't have to juggle mock
stacks individually. Each of the four handlers is named ``handler.py`` and lives
in its own directory; we load them via importlib under distinct module names
to avoid collisions.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
# Ensure the shared package is importable by handler code.
sys.path.insert(0, str(ROOT))


UPLOADS_TABLE = "test-uploads"
AUDIT_TABLE = "test-audit"
UPLOADS_BUCKET = "test-uploads-bucket"
EVENTS_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:filedrop-events"
SENDER_EMAIL = "sender@example.com"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("UPLOADS_TABLE", UPLOADS_TABLE)
    monkeypatch.setenv("AUDIT_TABLE", AUDIT_TABLE)
    monkeypatch.setenv("UPLOADS_BUCKET", UPLOADS_BUCKET)
    monkeypatch.setenv("EVENTS_TOPIC_ARN", EVENTS_TOPIC_ARN)
    monkeypatch.setenv("SENDER_EMAIL", SENDER_EMAIL)
    monkeypatch.setenv("POWERTOOLS_SERVICE_NAME", "filedrop-test")


def _load_handler(name: str) -> ModuleType:
    """Load functions/<name>/handler.py under module name filedrop_<name>."""
    path = ROOT / "functions" / name / "handler.py"
    module_name = f"filedrop_handler_{name}"
    # Force reload so env vars set inside the test are picked up.
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def load_handler():
    return _load_handler


@pytest.fixture
def aws():
    """Yields a live moto AWS mock with all Filedrop resources provisioned."""
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=UPLOADS_TABLE,
            KeySchema=[{"AttributeName": "upload_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "upload_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=AUDIT_TABLE,
            KeySchema=[
                {"AttributeName": "upload_id", "KeyType": "HASH"},
                {"AttributeName": "event_timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "upload_id", "AttributeType": "S"},
                {"AttributeName": "event_timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=UPLOADS_BUCKET)

        sns = boto3.client("sns", region_name="us-east-1")
        topic = sns.create_topic(Name="filedrop-events")
        os.environ["EVENTS_TOPIC_ARN"] = topic["TopicArn"]

        ses = boto3.client("ses", region_name="us-east-1")
        ses.verify_email_identity(EmailAddress=SENDER_EMAIL)

        yield {
            "ddb": ddb,
            "s3": s3,
            "sns": sns,
            "ses": ses,
            "topic_arn": topic["TopicArn"],
        }


@pytest.fixture
def lambda_ctx():
    """Minimal Lambda context that satisfies powertools' Logger + Tracer decorators."""
    return SimpleNamespace(
        function_name="test-fn",
        function_version="$LATEST",
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:test-fn",
        memory_limit_in_mb=128,
        aws_request_id="test-request-id",
        log_group_name="/aws/lambda/test-fn",
        log_stream_name="2026/01/01/[$LATEST]abcdef",
        identity=None,
        client_context=None,
        get_remaining_time_in_millis=lambda: 30_000,
    )


@pytest.fixture
def uploads_table(aws):
    return boto3.resource("dynamodb", region_name="us-east-1").Table(UPLOADS_TABLE)


@pytest.fixture
def audit_table(aws):
    return boto3.resource("dynamodb", region_name="us-east-1").Table(AUDIT_TABLE)
