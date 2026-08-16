"""Local (Docker-less) Lambda bundling.

CDK's default Lambda bundling shells into Docker with the SAM build image, which
requires Docker Desktop running locally. This module provides a ``BundlingOptions``
that tries a pure-Python local bundle first (copy code + pip install into the
output dir) and only falls back to Docker if the local run fails.

Same result either way — a directory with handler.py + shared/ + powertools —
just without the Docker dependency for the common case.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import jsii
from aws_cdk import aws_lambda as lambda_

LAMBDA_ROOT = Path(__file__).resolve().parent.parent


@jsii.implements(cdk.ILocalBundling)
class _LocalLambdaBundle:
    """Copies the function dir + shared/ into output_dir and pip-installs powertools."""

    def __init__(self, subdir: str) -> None:
        self._subdir = subdir

    def try_bundle(self, output_dir: str, options: Any) -> bool:  # noqa: ANN401
        try:
            src = LAMBDA_ROOT / "functions" / self._subdir
            shared = LAMBDA_ROOT / "shared"
            out = Path(output_dir)

            for item in src.iterdir():
                # Skip __pycache__ and other build-only artifacts.
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
            return True
        except Exception:  # noqa: BLE001
            # Returning False makes CDK fall back to Docker-based bundling.
            return False


def lambda_asset(subdir: str) -> lambda_.Code:
    """Return a Lambda Code asset built with local bundling (Docker fallback)."""
    return lambda_.Code.from_asset(
        str(LAMBDA_ROOT),
        bundling=cdk.BundlingOptions(
            image=lambda_.Runtime.PYTHON_3_12.bundling_image,
            local=_LocalLambdaBundle(subdir),
            # Same command Docker would run if local bundling fails.
            command=[
                "bash",
                "-c",
                " && ".join(
                    [
                        f"cp -r functions/{subdir}/. /asset-output/",
                        "cp -r shared /asset-output/shared",
                        "pip install --no-cache-dir aws-lambda-powertools[tracer]>=3.0.0 -t /asset-output",
                    ]
                ),
            ],
        ),
    )
