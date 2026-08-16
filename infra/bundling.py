"""Pre-build Lambda asset directories, then hand them to CDK as static assets.

The obvious approach — CDK ``BundlingOptions`` with a ``local`` bundle — hit
a persistent Windows EPERM race: after ``pip install`` returns, Windows keeps
file handles open on the freshly-written .pyc / metadata files (Defender + the
pip subprocess's finalizers), and CDK's atomic ``rename('...-building', '...')``
fails.

Working around it means building the assets outside ``cdk.out/`` entirely and
letting CDK see them as plain directories (no bundling, no atomic rename).
Cross-platform, no Docker, no flakiness.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from aws_cdk import aws_lambda as lambda_

LAMBDA_ROOT = Path(__file__).resolve().parent.parent
BUILD_ROOT = LAMBDA_ROOT / ".build" / "lambda"


def _build_asset(subdir: str) -> Path:
    """Assemble a Lambda deployment dir at .build/lambda/<subdir>.

    Idempotent per invocation: wipes the target, copies function code +
    shared package, then pip-installs powertools into it.
    """
    src = LAMBDA_ROOT / "functions" / subdir
    shared = LAMBDA_ROOT / "shared"
    out = BUILD_ROOT / subdir

    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        if item.name == "__pycache__":
            continue
        if item.is_dir():
            shutil.copytree(item, out / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, out / item.name)

    shutil.copytree(shared, out / "shared", dirs_exist_ok=True)

    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--quiet",
            "aws-lambda-powertools[tracer]>=3.0.0",
            "-t",
            str(out),
        ]
    )
    return out


def lambda_asset(subdir: str) -> lambda_.Code:
    """Prebuild the asset dir and return it as a Lambda ``Code``.

    CDK sees a normal directory — no bundling hook, no atomic rename.
    """
    asset_dir = _build_asset(subdir)
    return lambda_.Code.from_asset(str(asset_dir))
