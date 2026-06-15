import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


def load_invoke_db_migration():
    path = Path(__file__).with_name("invoke_db_migration.py")
    spec = importlib.util.spec_from_file_location("invoke_db_migration_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


invoke_db_migration = load_invoke_db_migration()


class InvokeDbMigrationTests(unittest.TestCase):
    def test_retries_transient_lambda_503_response(self):
        responses = [
            {
                "statusCode": 503,
                "body": json.dumps(
                    {"error": {"code": "SERVICE_UNAVAILABLE", "message": "Database temporarily unavailable"}}
                ),
            },
            {
                "statusCode": 200,
                "body": json.dumps({"data": {"status": "migrated", "migration_id": "schema-1"}}),
            },
        ]

        def fake_run(cmd, check, text, capture_output):
            Path(cmd[-1]).write_text(json.dumps(responses.pop(0)), encoding="utf-8")
            return subprocess_result(stdout="{}")

        with (
            mock.patch.object(invoke_db_migration.subprocess, "run", side_effect=fake_run) as run_mock,
            mock.patch.object(invoke_db_migration.time, "sleep") as sleep_mock,
        ):
            data = invoke_db_migration.invoke(
                "api",
                "us-east-1",
                max_attempts=2,
                retry_delay_seconds=0,
            )

        self.assertEqual(data["status"], "migrated")
        self.assertEqual(data["migration_id"], "schema-1")
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0)

    def test_does_not_retry_permanent_lambda_4xx_response(self):
        def fake_run(cmd, check, text, capture_output):
            Path(cmd[-1]).write_text(
                json.dumps({"statusCode": 409, "body": json.dumps({"error": {"code": "CONFLICT"}})}),
                encoding="utf-8",
            )
            return subprocess_result(stdout="{}")

        with mock.patch.object(invoke_db_migration.subprocess, "run", side_effect=fake_run) as run_mock:
            with self.assertRaisesRegex(RuntimeError, "statusCode=409"):
                invoke_db_migration.invoke(
                    "api",
                    "us-east-1",
                    max_attempts=3,
                    retry_delay_seconds=0,
                )

        self.assertEqual(run_mock.call_count, 1)


def subprocess_result(*, stdout="", stderr="", returncode=0):
    class Result:
        pass

    result = Result()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


if __name__ == "__main__":
    unittest.main()
