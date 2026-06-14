#!/usr/bin/env python3
"""Bootstrap dashboard admin access for the Cognito-protected dashboard."""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(cmd, *, input_text=None, allow_error=False):
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 and not allow_error:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def terraform_output(name):
    result = run(["terraform", "output", "-raw", name], allow_error=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def normalize_email(email):
    return email.strip().lower()


def admin_get_user(user_pool_id, username, region):
    result = run(
        [
            "aws",
            "cognito-idp",
            "admin-get-user",
            "--user-pool-id",
            user_pool_id,
            "--username",
            username,
            "--region",
            region,
            "--output",
            "json",
        ],
        allow_error=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def attr_value(user, name):
    for attr in user.get("UserAttributes", []):
        if attr.get("Name") == name:
            return attr.get("Value")
    return None


def ensure_cognito_user(user_pool_id, email, password, display_name, region):
    user = admin_get_user(user_pool_id, email, region)
    attrs = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    if display_name:
        attrs.append({"Name": "name", "Value": display_name})

    if user is None:
        args = [
            "aws",
            "cognito-idp",
            "admin-create-user",
            "--user-pool-id",
            user_pool_id,
            "--username",
            email,
            "--user-attributes",
            *[f"Name={item['Name']},Value={item['Value']}" for item in attrs],
            "--message-action",
            "SUPPRESS",
            "--region",
            region,
            "--output",
            "json",
        ]
        run(args)
    else:
        args = [
            "aws",
            "cognito-idp",
            "admin-update-user-attributes",
            "--user-pool-id",
            user_pool_id,
            "--username",
            email,
            "--user-attributes",
            *[f"Name={item['Name']},Value={item['Value']}" for item in attrs],
            "--region",
            region,
        ]
        run(args)

    run(
        [
            "aws",
            "cognito-idp",
            "admin-set-user-password",
            "--user-pool-id",
            user_pool_id,
            "--username",
            email,
            "--password",
            password,
            "--permanent",
            "--region",
            region,
        ]
    )
    user = admin_get_user(user_pool_id, email, region)
    if not user:
        raise RuntimeError("Cognito user was not found after create/update")
    return attr_value(user, "sub")


def invoke_bootstrap_lambda(function_name, payload, region, *, max_attempts=6, retry_delay_seconds=10):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(payload, tmp)
        tmp_path = Path(tmp.name)

    out_path = tmp_path.with_suffix(".out.json")
    try:
        for attempt in range(1, max_attempts + 1):
            run(
                [
                    "aws",
                    "lambda",
                    "invoke",
                    "--function-name",
                    function_name,
                    "--payload",
                    f"file://{tmp_path}",
                    "--cli-binary-format",
                    "raw-in-base64-out",
                    "--region",
                    region,
                    str(out_path),
                ]
            )
            body = json.loads(out_path.read_text())
            status_code = body.get("statusCode", 200) if isinstance(body, dict) else 200
            if not isinstance(status_code, int) or status_code < 400:
                return body
            if status_code < 500 or attempt == max_attempts:
                raise RuntimeError(f"bootstrap lambda returned error: {body}")
            time.sleep(retry_delay_seconds)

        raise RuntimeError("bootstrap lambda did not return a response")
    finally:
        tmp_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Bootstrap dashboard admin access.")
    parser.add_argument("--email", required=True, help="Bootstrap admin email.")
    parser.add_argument("--password", default="", help="Optional permanent Cognito password.")
    parser.add_argument("--display-name", default="Bootstrap Admin")
    parser.add_argument("--alert-email", default="", help="Optional SNS summary alert email.")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--user-pool-id", default="")
    parser.add_argument("--lambda-function", default="")
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--retry-delay-seconds", type=float, default=10)
    args = parser.parse_args()

    email = normalize_email(args.email)
    if "@" not in email:
        raise SystemExit("bootstrap email must be a valid email address")
    alert_email = normalize_email(args.alert_email) if args.alert_email else ""
    if alert_email and "@" not in alert_email:
        raise SystemExit("bootstrap alert email must be a valid email address")

    user_pool_id = args.user_pool_id or terraform_output("cognito_user_pool_id") or terraform_output("user_pool_id")
    function_name = args.lambda_function or terraform_output("api_lambda_name")
    if not function_name:
        raise SystemExit("api lambda function name is required or must be available as terraform output api_lambda_name")

    cognito_sub = None
    if args.password:
        if not user_pool_id:
            raise SystemExit("cognito user pool id is required when BOOTSTRAP_PASSWORD is provided")
        cognito_sub = ensure_cognito_user(
            user_pool_id=user_pool_id,
            email=email,
            password=args.password,
            display_name=args.display_name,
            region=args.region,
        )

    payload = {
        "action": "bootstrap_dashboard_admin",
        "email": email,
        "display_name": args.display_name,
        "cognito_sub": cognito_sub,
        "alert_email": alert_email,
    }
    body = invoke_bootstrap_lambda(
        function_name,
        payload,
        args.region,
        max_attempts=max(1, args.max_attempts),
        retry_delay_seconds=max(0, args.retry_delay_seconds),
    )
    print(json.dumps(body, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
