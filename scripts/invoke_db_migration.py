#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


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


def _status_code(payload):
    try:
        return int(payload.get("statusCode", 0))
    except (TypeError, ValueError):
        return 0


def _invoke_once(function_name, region):
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

    return metadata, payload


def invoke(function_name, region, *, max_attempts=8, retry_delay_seconds=10, max_retry_delay_seconds=60):
    delay_seconds = retry_delay_seconds

    for attempt in range(1, max_attempts + 1):
        metadata, payload = _invoke_once(function_name, region)

        if metadata.get("FunctionError"):
            raise RuntimeError(f"API Lambda migration action failed: {json.dumps(payload, sort_keys=True)}")

        status_code = _status_code(payload)
        if 200 <= status_code < 300:
            body = payload.get("body")
            body_payload = json.loads(body) if isinstance(body, str) and body else {}
            return body_payload.get("data", {})

        message = f"API Lambda migration action returned statusCode={status_code}: {json.dumps(payload, sort_keys=True)}"
        if status_code not in RETRYABLE_STATUS_CODES or attempt == max_attempts:
            raise RuntimeError(message)

        print(
            f"db migration attempt {attempt}/{max_attempts} returned statusCode={status_code}; "
            f"retrying in {delay_seconds:g}s",
            file=sys.stderr,
        )
        time.sleep(delay_seconds)
        delay_seconds = min(delay_seconds * 2, max_retry_delay_seconds)

    raise RuntimeError("API Lambda migration action did not return a response")


def main():
    parser = argparse.ArgumentParser(description="Invoke the API Lambda direct RDS schema migration action and fail on migration errors.")
    parser.add_argument("--function", help="API Lambda function name. Defaults to Terraform output api_lambda_name.")
    parser.add_argument("--region", default="us-east-1", help="AWS region.")
    parser.add_argument("--max-attempts", type=int, default=8, help="Maximum migration invoke attempts for transient Lambda responses.")
    parser.add_argument("--retry-delay-seconds", type=float, default=10, help="Initial delay between transient migration retries.")
    parser.add_argument("--max-retry-delay-seconds", type=float, default=60, help="Maximum delay between transient migration retries.")
    args = parser.parse_args()

    try:
        function_name = args.function or terraform_output("api_lambda_name")
        data = invoke(
            function_name,
            args.region,
            max_attempts=max(1, args.max_attempts),
            retry_delay_seconds=max(0, args.retry_delay_seconds),
            max_retry_delay_seconds=max(0, args.max_retry_delay_seconds),
        )
    except Exception as exc:
        print(f"db migration failed: {exc}", file=sys.stderr)
        return 1

    migration_id = data.get("migration_id", "unknown")
    status = data.get("status", "unknown")
    print(f"db migration {status}: {migration_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
