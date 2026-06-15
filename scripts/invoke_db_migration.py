#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _load_json(path):
    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Lambda payload was not valid JSON: {raw}") from exc


def terraform_output(name):
    completed = subprocess.run(
        ["terraform", "output", "-raw", name],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"terraform output {name} failed")
    value = completed.stdout.strip()
    if not value:
        raise RuntimeError(f"terraform output {name} was empty")
    return value


def invoke(function_name, region):
    request_payload = json.dumps({"action": "migrate_database_schema"})
    with tempfile.NamedTemporaryFile(prefix="db-migration-", suffix=".json") as payload_file:
        cmd = [
            "aws",
            "lambda",
            "invoke",
            "--function-name",
            function_name,
            "--region",
            region,
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            request_payload,
            payload_file.name,
        ]
        completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "aws lambda invoke failed")

        metadata = json.loads(completed.stdout or "{}")
        payload = _load_json(payload_file.name)

    if metadata.get("FunctionError"):
        raise RuntimeError(f"API Lambda migration action failed: {json.dumps(payload, sort_keys=True)}")

    status_code = int(payload.get("statusCode", 0))
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"API Lambda migration action returned statusCode={status_code}: {json.dumps(payload, sort_keys=True)}")

    body = payload.get("body")
    body_payload = json.loads(body) if isinstance(body, str) and body else {}
    return body_payload.get("data", {})


def main():
    parser = argparse.ArgumentParser(description="Invoke the API Lambda direct RDS schema migration action and fail on migration errors.")
    parser.add_argument("--function", help="API Lambda function name. Defaults to Terraform output api_lambda_name.")
    parser.add_argument("--region", default="us-east-1", help="AWS region.")
    args = parser.parse_args()

    try:
        function_name = args.function or terraform_output("api_lambda_name")
        data = invoke(function_name, args.region)
    except Exception as exc:
        print(f"db migration failed: {exc}", file=sys.stderr)
        return 1

    migration_id = data.get("migration_id", "unknown")
    status = data.get("status", "unknown")
    print(f"db migration {status}: {migration_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
