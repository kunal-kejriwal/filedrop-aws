"""CDK app entrypoint.

Reads region + sender email from CDK context (see ``cdk.context.json.example``)
so nothing about the account or identity is hardcoded here.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from infra.api_stack import FiledropApiStack
from infra.core_stack import FiledropCoreStack


def _require_context(app: cdk.App, key: str, default: str | None = None) -> str:
    value = app.node.try_get_context(key) or default
    if not value:
        raise SystemExit(
            f"Missing required context '{key}'. "
            "Copy cdk.context.json.example → cdk.context.json and fill it in."
        )
    return str(value)


def main() -> None:
    app = cdk.App()

    region = _require_context(app, "region", default="ap-south-1")
    sender_email = _require_context(app, "senderEmail")
    alarm_email = app.node.try_get_context("alarmEmail")

    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=region,
    )

    core = FiledropCoreStack(
        app,
        "FiledropCoreStack",
        env=env,
        sender_email=sender_email,
        alarm_email=alarm_email,
    )

    FiledropApiStack(
        app,
        "FiledropApiStack",
        env=env,
        uploads_bucket=core.uploads_bucket,
        uploads_table=core.uploads_table,
    )

    app.synth()


if __name__ == "__main__":
    main()
