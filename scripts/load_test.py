"""End-to-end load test for a deployed Filedrop stack.

Loops N times:
    1. POST /uploads → get presigned PUT URL.
    2. PUT a small generated payload directly to S3.
    3. Poll the audit DynamoDB table for the NOTIFIED event (or use --wait to
       accept a fixed delay before scanning).

Measures upload-completion → NOTIFIED latency per file and writes a summary
to load_test_results.json. This is deliberately blunt — it's here so the
README's "the numbers" table has a real source, not to be a benchmark suite.

Requires:
    * FILEDROP_API_URL (or --api-url)
    * TEST_EMAIL — must be SES-verified while SES is in sandbox
    * AWS credentials with dynamodb:Query on the audit table
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import boto3
import requests

AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "FiledropCoreStack-AuditTable")


@dataclass
class Result:
    upload_id: str
    status: str = "pending"
    upload_finished_at: str | None = None
    notified_at: str | None = None
    latency_seconds: float | None = None
    error: str | None = None


@dataclass
class Summary:
    total: int = 0
    ok: int = 0
    failed: int = 0
    duplicate_suppressed: int = 0
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None
    results: list[Result] = field(default_factory=list)


def _one_upload(*, api_url: str, email: str, index: int) -> Result:
    filename = f"loadtest-{index}-{uuid.uuid4().hex[:8]}.txt"
    body = f"filedrop load test payload {index} at {datetime.now(UTC).isoformat()}\n".encode()

    r = requests.post(
        f"{api_url.rstrip('/')}/uploads",
        json={
            "email": email,
            "filename": filename,
            "content_type": "text/plain",
            "size_bytes": len(body),
        },
        timeout=10,
    )
    r.raise_for_status()
    slot = r.json()
    result = Result(upload_id=slot["upload_id"])

    put = requests.put(
        slot["upload_url"],
        data=body,
        headers={"Content-Type": "text/plain"},
        timeout=30,
    )
    put.raise_for_status()
    result.upload_finished_at = datetime.now(UTC).isoformat()
    result.status = "uploaded"
    return result


def _poll_notified(ddb, results: list[Result], wait_seconds: int) -> None:
    """Query the audit table for each upload's file_uploaded event.

    Falls back to polling until wait_seconds elapses, then gives up.
    """
    deadline = time.time() + wait_seconds
    outstanding = {r.upload_id: r for r in results if r.status == "uploaded"}
    while outstanding and time.time() < deadline:
        for upload_id in list(outstanding.keys()):
            resp = ddb.query(
                TableName=AUDIT_TABLE,
                KeyConditionExpression="upload_id = :uid",
                ExpressionAttributeValues={":uid": {"S": upload_id}},
            )
            for item in resp.get("Items", []):
                if item.get("event_type", {}).get("S") == "file_uploaded":
                    r = outstanding.pop(upload_id)
                    r.notified_at = item["event_timestamp"]["S"]
                    upload_ts = datetime.fromisoformat(r.upload_finished_at)  # type: ignore[arg-type]
                    notify_ts = datetime.fromisoformat(r.notified_at)
                    r.latency_seconds = (notify_ts - upload_ts).total_seconds()
                    r.status = "notified"
                    break
        if outstanding:
            time.sleep(2)


def _summarize(results: list[Result]) -> Summary:
    summary = Summary(total=len(results), results=results)
    latencies: list[float] = []
    for r in results:
        if r.status == "notified" and r.latency_seconds is not None:
            summary.ok += 1
            latencies.append(r.latency_seconds)
        elif r.error:
            summary.failed += 1
    if latencies:
        latencies.sort()
        summary.p50 = statistics.median(latencies)
        summary.p95 = latencies[max(0, int(0.95 * len(latencies)) - 1)]
        summary.p99 = latencies[max(0, int(0.99 * len(latencies)) - 1)]
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--api-url", default=os.environ.get("FILEDROP_API_URL"))
    ap.add_argument("--email", default=os.environ.get("TEST_EMAIL"))
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--wait", type=int, default=120, help="seconds to poll audit table")
    ap.add_argument("--audit-table", default=os.environ.get("AUDIT_TABLE"))
    args = ap.parse_args()

    if not args.api_url or not args.email:
        raise SystemExit("--api-url and --email (or env vars) required")
    if args.audit_table:
        global AUDIT_TABLE
        AUDIT_TABLE = args.audit_table

    print(f"running {args.count} uploads against {args.api_url}")
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [
            ex.submit(_one_upload, api_url=args.api_url, email=args.email, index=i)
            for i in range(args.count)
        ]
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                results.append(Result(upload_id="-", status="failed", error=str(e)))

    print(f"uploads complete; polling audit table for up to {args.wait}s")
    ddb = boto3.client("dynamodb")
    _poll_notified(ddb, results, wait_seconds=args.wait)

    summary = _summarize(results)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "api_url": args.api_url,
        "count": args.count,
        "ok": summary.ok,
        "failed": summary.failed,
        "p50_s": summary.p50,
        "p95_s": summary.p95,
        "p99_s": summary.p99,
        "results": [asdict(r) for r in summary.results],
    }
    with open("load_test_results.json", "w") as fh:
        json.dump(payload, fh, indent=2)

    print(
        "results: "
        f"ok={summary.ok}/{summary.total} "
        f"p50={summary.p50} p95={summary.p95} p99={summary.p99}"
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
