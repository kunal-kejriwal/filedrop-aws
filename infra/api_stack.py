"""API stack: HTTP API Gateway fronting the request_upload + get_upload_status Lambdas.

Separated from the core stack so we can iterate on the public surface without
touching storage or event plumbing. Cross-stack references (bucket, tables) are
passed in via the constructor so CDK produces proper Fn::ImportValue exports.

Routes:
    POST /uploads                    → request_upload (create slot + presigned PUT URL)
    GET  /uploads/{upload_id}/status → get_upload_status (poll for UPLOADED + presigned GET)
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_int,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
)
from constructs import Construct

from infra.bundling import lambda_asset


class FiledropApiStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        uploads_bucket: s3.IBucket,
        uploads_table: ddb.ITable,
        email_index_table: ddb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------------------------------------------------- request_upload
        request_fn = lambda_.Function(
            self,
            "RequestUploadFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_asset("request_upload"),
            timeout=Duration.seconds(10),
            memory_size=192,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "POWERTOOLS_SERVICE_NAME": "filedrop",
                "POWERTOOLS_LOG_LEVEL": "INFO",
                "UPLOADS_TABLE": uploads_table.table_name,
                "UPLOADS_BUCKET": uploads_bucket.bucket_name,
                "EMAIL_INDEX_TABLE": email_index_table.table_name,
            },
        )

        # IAM: PutItem on uploads + email-index, PutObject on uploads/ prefix only.
        uploads_table.grant_write_data(request_fn)
        # Conditional put on email-index as the 24h-cooldown gate for the demo.
        email_index_table.grant_write_data(request_fn)
        request_fn.add_to_role_policy(
            iam.PolicyStatement(
                # Presigning a PUT URL requires the caller to hold s3:PutObject on the
                # target key. Scoped to uploads/* so a compromised signing key can only
                # ever produce URLs under that prefix.
                actions=["s3:PutObject"],
                resources=[uploads_bucket.arn_for_objects("uploads/*")],
            )
        )

        # -------------------------------------------------- get_upload_status
        # Polled by the browser demo to discover when the file has finished
        # processing and to fetch a fresh presigned GET URL. Returns:
        #   {status, filename?, size_bytes?, download_url?, expires_at?}
        # Only returns download_url when status == UPLOADED (or NOTIFIED).
        status_fn = lambda_.Function(
            self,
            "GetUploadStatusFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_asset("get_upload_status"),
            timeout=Duration.seconds(10),
            memory_size=192,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "POWERTOOLS_SERVICE_NAME": "filedrop",
                "POWERTOOLS_LOG_LEVEL": "INFO",
                "UPLOADS_TABLE": uploads_table.table_name,
                "UPLOADS_BUCKET": uploads_bucket.bucket_name,
            },
        )
        uploads_table.grant_read_data(status_fn)
        status_fn.add_to_role_policy(
            iam.PolicyStatement(
                # s3:GetObject is what presigning a GET URL requires.
                actions=["s3:GetObject"],
                resources=[uploads_bucket.arn_for_objects("uploads/*")],
            )
        )

        # -------------------------------------------------- HTTP API + routes
        http_api = apigwv2.HttpApi(
            self,
            "FiledropHttpApi",
            api_name="filedrop-api",
            # CORS wide open so the site (kunalships.dev) + local dev can call it.
            # In a real product this would be scoped to the actual site origins.
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_headers=["content-type"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_origins=["*"],
                max_age=Duration.hours(1),
            ),
        )
        http_api.add_routes(
            path="/uploads",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_int.HttpLambdaIntegration("RequestUploadIntegration", request_fn),
        )
        http_api.add_routes(
            path="/uploads/{upload_id}/status",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_int.HttpLambdaIntegration("GetUploadStatusIntegration", status_fn),
        )

        cdk.CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
