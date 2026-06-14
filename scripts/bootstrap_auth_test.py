import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


def load_bootstrap_auth():
    path = Path(__file__).with_name("bootstrap_auth.py")
    spec = importlib.util.spec_from_file_location("bootstrap_auth_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap_auth = load_bootstrap_auth()


class BootstrapAuthInvokeTests(unittest.TestCase):
    def test_retries_transient_lambda_5xx_response(self):
        responses = [
            {"statusCode": 500, "body": '{"error": {"code": "INTERNAL_ERROR"}}'},
            {"statusCode": 200, "body": '{"data": {"status": "ok"}}'},
        ]

        def fake_run(cmd):
            Path(cmd[-1]).write_text(json.dumps(responses.pop(0)))

        with mock.patch.object(bootstrap_auth, "run", side_effect=fake_run) as run_mock:
            body = bootstrap_auth.invoke_bootstrap_lambda(
                "api",
                {"action": "bootstrap_dashboard_admin"},
                "us-east-1",
                max_attempts=2,
                retry_delay_seconds=0,
            )

        self.assertEqual(body["statusCode"], 200)
        self.assertEqual(run_mock.call_count, 2)

    def test_does_not_retry_lambda_4xx_response(self):
        def fake_run(cmd):
            Path(cmd[-1]).write_text(json.dumps({"statusCode": 409, "body": "{}"}))

        with mock.patch.object(bootstrap_auth, "run", side_effect=fake_run) as run_mock:
            with self.assertRaisesRegex(RuntimeError, "bootstrap lambda returned error"):
                bootstrap_auth.invoke_bootstrap_lambda(
                    "api",
                    {"action": "bootstrap_dashboard_admin"},
                    "us-east-1",
                    max_attempts=3,
                    retry_delay_seconds=0,
                )

        self.assertEqual(run_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
