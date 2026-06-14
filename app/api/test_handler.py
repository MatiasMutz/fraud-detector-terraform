import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path


def load_handler():
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2_extras = types.ModuleType("psycopg2.extras")
    fake_psycopg2_extras.RealDictCursor = object
    fake_psycopg2.extras = fake_psycopg2_extras
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *args, **kwargs: object()
    fake_botocore = types.ModuleType("botocore")
    fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
    fake_botocore_exceptions.ClientError = Exception

    sys.modules.setdefault("psycopg2", fake_psycopg2)
    sys.modules.setdefault("psycopg2.extras", fake_psycopg2_extras)
    sys.modules.setdefault("boto3", fake_boto3)
    sys.modules.setdefault("botocore", fake_botocore)
    sys.modules.setdefault("botocore.exceptions", fake_botocore_exceptions)

    path = Path(__file__).with_name("handler.py")
    spec = importlib.util.spec_from_file_location("api_handler_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApiTraceLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler()

    def test_trace_id_header_is_reused(self):
        event = {"headers": {"X-Trace-Id": "11111111-1111-4111-8111-111111111111"}}
        self.assertEqual(self.handler._request_trace_id(event), "11111111-1111-4111-8111-111111111111")

    def test_trace_id_filter_is_added_to_where_clause(self):
        where, params = self.handler._build_where({"trace_id": "11111111-1111-4111-8111-111111111111"})
        self.assertEqual(where, "WHERE trace_id = %s")
        self.assertEqual(params, ["11111111-1111-4111-8111-111111111111"])

    def test_unsafe_trace_id_filter_is_ignored(self):
        where, params = self.handler._build_where({"trace_id": "tx-secret"})
        self.assertEqual(where, "")
        self.assertEqual(params, [])

    def test_response_gets_trace_header(self):
        response = self.handler._with_trace({"statusCode": 200, "headers": {}, "body": "{}"}, "trace-1")
        self.assertEqual(response["headers"]["X-Trace-Id"], "trace-1")

    def test_user_behavior_route_template_wins_before_user_detail(self):
        route = self.handler._route_template("/users/usr-1/behavior", {"id": "usr-1"})
        self.assertEqual(route, "/users/{id}/behavior")

    def test_user_behavior_profile_deserializes_dynamodb_item(self):
        profile = self.handler._serialize_user_behavior_profile(
            "usr-1",
            {
                "user_id": {"S": "usr-1"},
                "avg_amount": {"N": "125.75"},
                "std_dev_amount": {"N": "12.5"},
                "tx_count": {"N": "8"},
                "tx_last_hour": {"N": "3"},
                "tx_last_10min": {"N": "2"},
                "typical_countries": {"L": [{"S": "AR"}, {"S": "UY"}]},
                "typical_channels": {"L": [{"S": "web"}]},
                "known_destinations": {"L": [{"S": "acct-1"}]},
                "last_country": {"S": "AR"},
                "last_timestamp": {"S": "2026-06-14T12:00:00Z"},
            },
        )

        self.assertTrue(profile["has_profile"])
        self.assertEqual(profile["user_id"], "usr-1")
        self.assertEqual(profile["avg_amount"], 125.75)
        self.assertEqual(profile["tx_count"], 8)
        self.assertEqual(profile["typical_countries"], ["AR", "UY"])
        self.assertEqual(profile["known_destinations"], ["acct-1"])

    def test_user_behavior_profile_missing_item_returns_empty_profile(self):
        profile = self.handler._serialize_user_behavior_profile("usr-new", None)
        self.assertFalse(profile["has_profile"])
        self.assertEqual(profile["user_id"], "usr-new")
        self.assertEqual(profile["tx_count"], 0)
        self.assertEqual(profile["typical_countries"], [])

    def test_safe_log_omits_sensitive_fields(self):
        logger = logging.getLogger()
        with self.assertLogs(logger, level="INFO") as captured:
            self.handler._safe_log(
                "api_request_completed",
                trace_id="trace-safe",
                transaction_id="tx-secret",
                user_id="user-secret",
                amount=42.5,
                query={"user_id": "user-secret"},
                route="/transactions/{id}",
            )

        payload = json.loads(captured.output[-1].split("INFO:root:", 1)[-1])
        encoded = json.dumps(payload)
        self.assertEqual(payload["trace_id"], "trace-safe")
        self.assertNotIn("tx-secret", encoded)
        self.assertNotIn("user-secret", encoded)
        self.assertNotIn("amount", payload)
        self.assertNotIn("query", payload)

    def test_schema_migration_backfills_dashboard_access_columns(self):
        class FakeCursor:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def execute(self, statement, params=None):
                self.statements.append(statement)

        class FakeConnection:
            def __init__(self):
                self.cursor_instance = FakeCursor()
                self.committed = False

            def cursor(self, *args, **kwargs):
                return self.cursor_instance

            def commit(self):
                self.committed = True

        conn = FakeConnection()
        self.handler._ensure_schema(conn)
        statement = conn.cursor_instance.statements[0]

        self.assertIn("ADD COLUMN IF NOT EXISTS is_bootstrap_admin BOOLEAN", statement)
        self.assertIn("ADD COLUMN IF NOT EXISTS role TEXT", statement)
        self.assertIn("ADD COLUMN IF NOT EXISTS status TEXT", statement)
        self.assertIn("idx_dashboard_access_email_normalized_unique", statement)
        self.assertTrue(conn.committed)


if __name__ == "__main__":
    unittest.main()
