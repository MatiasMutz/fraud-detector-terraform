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
    fake_psycopg2.OperationalError = type("OperationalError", (Exception,), {})
    fake_psycopg2.DatabaseError = type("DatabaseError", (Exception,), {})
    fake_psycopg2_extras.RealDictCursor = object
    fake_psycopg2.extras = fake_psycopg2_extras
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *args, **kwargs: object()
    fake_botocore = types.ModuleType("botocore")
    fake_botocore_config = types.ModuleType("botocore.config")
    fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
    fake_botocore_config.Config = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    fake_botocore_exceptions.BotoCoreError = type("BotoCoreError", (Exception,), {})
    fake_botocore_exceptions.ClientError = type("ClientError", (Exception,), {})

    sys.modules["psycopg2"] = fake_psycopg2
    sys.modules["psycopg2.extras"] = fake_psycopg2_extras
    sys.modules["boto3"] = fake_boto3
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.config"] = fake_botocore_config
    sys.modules["botocore.exceptions"] = fake_botocore_exceptions

    path = Path(__file__).with_name("handler.py")
    spec = importlib.util.spec_from_file_location("api_handler_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApiTraceLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler()

    def setUp(self):
        self.handler._db_credentials = None

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

    def test_load_db_credentials_fetches_secret_once(self):
        class FakeSecretsManager:
            def __init__(self):
                self.calls = []

            def get_secret_value(self, **kwargs):
                self.calls.append(kwargs)
                return {"SecretString": json.dumps({"username": "fraud_admin", "password": "secret"})}

        client = FakeSecretsManager()
        original_aws_client = self.handler._aws_client
        original_secret_arn = self.handler.os.environ.get("DB_CREDENTIALS_SECRET_ARN")
        self.handler._aws_client = lambda service_name: client
        self.handler.os.environ["DB_CREDENTIALS_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:db"
        )
        try:
            credentials = self.handler._load_db_credentials()
            cached = self.handler._load_db_credentials()
        finally:
            self.handler._aws_client = original_aws_client
            if original_secret_arn is None:
                self.handler.os.environ.pop("DB_CREDENTIALS_SECRET_ARN", None)
            else:
                self.handler.os.environ["DB_CREDENTIALS_SECRET_ARN"] = original_secret_arn

        self.assertEqual(credentials, {"username": "fraud_admin", "password": "secret"})
        self.assertIs(cached, credentials)
        self.assertEqual(client.calls, [{"SecretId": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db"}])

    def test_load_db_credentials_requires_secret_arn(self):
        original_secret_arn = self.handler.os.environ.get("DB_CREDENTIALS_SECRET_ARN")
        self.handler.os.environ.pop("DB_CREDENTIALS_SECRET_ARN", None)
        try:
            with self.assertRaises(self.handler.DBSecretConfigError):
                self.handler._load_db_credentials()
        finally:
            if original_secret_arn is not None:
                self.handler.os.environ["DB_CREDENTIALS_SECRET_ARN"] = original_secret_arn

    def test_load_db_credentials_rejects_incomplete_secret(self):
        class FakeSecretsManager:
            def get_secret_value(self, **kwargs):
                return {"SecretString": json.dumps({"username": "fraud_admin"})}

        original_aws_client = self.handler._aws_client
        original_secret_arn = self.handler.os.environ.get("DB_CREDENTIALS_SECRET_ARN")
        self.handler._aws_client = lambda service_name: FakeSecretsManager()
        self.handler.os.environ["DB_CREDENTIALS_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:db"
        )
        try:
            with self.assertRaises(self.handler.DBSecretConfigError):
                self.handler._load_db_credentials()
        finally:
            self.handler._aws_client = original_aws_client
            if original_secret_arn is None:
                self.handler.os.environ.pop("DB_CREDENTIALS_SECRET_ARN", None)
            else:
                self.handler.os.environ["DB_CREDENTIALS_SECRET_ARN"] = original_secret_arn

    def test_handler_logs_request_start_before_dispatch(self):
        def fake_dispatch(event, path, query, path_params, method):
            return self.handler._ok({"status": "ok"})

        event = {
            "rawPath": "/dashboard/me",
            "headers": {"X-Trace-Id": "11111111-1111-4111-8111-111111111111"},
            "requestContext": {
                "requestId": "req-1",
                "http": {"method": "GET"},
            },
        }

        original_dispatch = self.handler._dispatch_request
        self.handler._dispatch_request = fake_dispatch
        try:
            logger = logging.getLogger()
            with self.assertLogs(logger, level="INFO") as captured:
                response = self.handler.handler(event, None)
        finally:
            self.handler._dispatch_request = original_dispatch

        self.assertEqual(response["statusCode"], 200)
        start_payload = json.loads(captured.output[0].split("INFO:root:", 1)[-1])
        self.assertEqual(start_payload["action"], "api_request_started")
        self.assertEqual(start_payload["route"], "/dashboard/me")
        self.assertEqual(start_payload["method"], "GET")

    def test_direct_migration_action_applies_schema(self):
        class FakeCursor:
            def __init__(self):
                self.executions = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def execute(self, statement, params=None):
                self.executions.append((statement, params))

        class FakeConnection:
            def __init__(self, kwargs):
                self.kwargs = kwargs
                self.cursor_instance = FakeCursor()
                self.committed = False
                self.commit_count = 0
                self.rolled_back = False
                self.closed = False

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.committed = True
                self.commit_count += 1

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        connection = None

        def fake_connect(**kwargs):
            nonlocal connection
            connection = FakeConnection(kwargs)
            return connection

        original_connect = getattr(self.handler.psycopg2, "connect", None)
        original_load_db_credentials = self.handler._load_db_credentials
        self.handler.psycopg2.connect = fake_connect
        self.handler._load_db_credentials = lambda: {"username": "fraud_admin", "password": "secret"}
        self.handler.os.environ.update(
            {
                "DB_HOST": "db.example",
                "DB_PORT": "5432",
                "DB_NAME": "fraud_results",
                "DB_CREDENTIALS_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db",
            }
        )
        try:
            response = self.handler.handler({"action": "migrate_database_schema"}, None)
        finally:
            self.handler._load_db_credentials = original_load_db_credentials
            if original_connect is None:
                delattr(self.handler.psycopg2, "connect")
            else:
                self.handler.psycopg2.connect = original_connect

        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["data"]["status"], "migrated")
        self.assertEqual(body["data"]["migration_id"], self.handler.MIGRATION_ID)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        self.assertFalse(connection.rolled_back)
        self.assertEqual(connection.kwargs["application_name"], "fraud-detector-api-migration")
        self.assertNotIn("options", connection.kwargs)
        statements = connection.cursor_instance.executions
        self.assertEqual(statements[0], ("SET SESSION statement_timeout = %s", (25000,)))
        self.assertEqual(statements[1], ("SET SESSION lock_timeout = %s", (10000,)))
        self.assertEqual(statements[2], ("SET SESSION idle_in_transaction_session_timeout = %s", (25000,)))
        self.assertIn("CREATE TABLE IF NOT EXISTS transactions", statements[3][0])
        self.assertIn("CREATE TABLE IF NOT EXISTS dashboard_access", statements[3][0])
        self.assertIn("CREATE TABLE IF NOT EXISTS schema_migrations", statements[3][0])
        self.assertIn("INSERT INTO schema_migrations", statements[4][0])
        self.assertEqual(statements[4][1], (self.handler.MIGRATION_ID,))
        self.assertEqual(connection.commit_count, 2)

    def test_http_body_action_does_not_trigger_migration(self):
        def fail_migration():
            raise AssertionError("migration should not run for HTTP request bodies")

        def fake_dispatch(event, path, query, path_params, method):
            return self.handler._ok({"route": path, "method": method})

        event = {
            "rawPath": "/dashboard/me",
            "body": json.dumps({"action": "migrate_database_schema"}),
            "requestContext": {"requestId": "req-2", "http": {"method": "POST"}},
        }

        original_migration = self.handler._migrate_database_schema
        original_dispatch = self.handler._dispatch_request
        self.handler._migrate_database_schema = fail_migration
        self.handler._dispatch_request = fake_dispatch
        try:
            response = self.handler.handler(event, None)
        finally:
            self.handler._migrate_database_schema = original_migration
            self.handler._dispatch_request = original_dispatch

        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["data"]["route"], "/dashboard/me")

if __name__ == "__main__":
    unittest.main()
