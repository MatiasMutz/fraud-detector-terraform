import decimal
import json
import logging
import os
import re
import uuid
from time import perf_counter
from urllib.parse import unquote

import boto3
import psycopg2
import psycopg2.extras
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization,content-type,x-cognito-access-token,x-trace-id",
    "Access-Control-Allow-Methods": "DELETE,GET,OPTIONS,POST,PUT",
    "Access-Control-Max-Age": "300",
}

_conn = None
_dynamodb_client = None
_db_credentials = None
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SORTABLE_TX = {
    "transaction_id": "transaction_id",
    "user_id": "user_id",
    "amount": "amount",
    "currency": "currency",
    "country": "country",
    "channel": "channel",
    "fraud_score": "fraud_score",
    "is_fraud": "is_fraud",
    "decision": "decision",
    "processed_at": "processed_at",
    "trace_id": "trace_id",
    "ingested_at": "ingested_at",
}
_SORTABLE_USERS = {
    "user_id": "user_id",
    "total_transactions": "total_transactions",
    "fraud_count": "fraud_count",
    "avg_fraud_score": "avg_fraud_score",
    "last_seen": "last_seen",
    "fraud_rate_pct": "fraud_rate_pct",
}

MIGRATION_ID = "2026_06_15_001_dashboard_api_schema"


class DBSecretConfigError(RuntimeError):
    pass


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS transactions (
    id             SERIAL PRIMARY KEY,
    transaction_id VARCHAR(255) UNIQUE NOT NULL,
    user_id        VARCHAR(255),
    amount         NUMERIC(15, 2),
    currency       VARCHAR(10),
    country        VARCHAR(100),
    channel        VARCHAR(50),
    fraud_score    FLOAT,
    is_fraud       BOOLEAN,
    decision       VARCHAR(20),
    processed_at   TIMESTAMPTZ DEFAULT NOW(),
    trace_id       VARCHAR(128),
    ingested_at    TIMESTAMPTZ
);

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_tx_processed_at ON transactions (processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_tx_is_fraud     ON transactions (is_fraud);
CREATE INDEX IF NOT EXISTS idx_tx_user_id      ON transactions (user_id);
CREATE INDEX IF NOT EXISTS idx_tx_country      ON transactions (country);
CREATE INDEX IF NOT EXISTS idx_tx_channel      ON transactions (channel);
CREATE INDEX IF NOT EXISTS idx_tx_trace_id     ON transactions (trace_id);
CREATE INDEX IF NOT EXISTS idx_tx_ingested_at  ON transactions (ingested_at DESC);

CREATE TABLE IF NOT EXISTS dashboard_access (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email              TEXT NOT NULL,
    email_normalized   TEXT NOT NULL UNIQUE,
    display_name       TEXT,
    role               TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    status             TEXT NOT NULL CHECK (status IN ('pending', 'active', 'disabled')),
    cognito_sub        TEXT,
    invited_by_email   TEXT,
    is_bootstrap_admin BOOLEAN NOT NULL DEFAULT FALSE,
    invited_at         TIMESTAMPTZ,
    activated_at       TIMESTAMPTZ,
    disabled_at        TIMESTAMPTZ,
    last_login_at      TIMESTAMPTZ,
    summary_sns_subscription_arn        TEXT,
    summary_sns_subscription_status     TEXT,
    summary_sns_subscription_warning    TEXT,
    summary_sns_subscription_updated_at TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE dashboard_access
    ADD COLUMN IF NOT EXISTS email TEXT,
    ADD COLUMN IF NOT EXISTS email_normalized TEXT,
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS role TEXT,
    ADD COLUMN IF NOT EXISTS status TEXT,
    ADD COLUMN IF NOT EXISTS cognito_sub TEXT,
    ADD COLUMN IF NOT EXISTS invited_by_email TEXT,
    ADD COLUMN IF NOT EXISTS is_bootstrap_admin BOOLEAN,
    ADD COLUMN IF NOT EXISTS invited_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS summary_sns_subscription_arn TEXT,
    ADD COLUMN IF NOT EXISTS summary_sns_subscription_status TEXT,
    ADD COLUMN IF NOT EXISTS summary_sns_subscription_warning TEXT,
    ADD COLUMN IF NOT EXISTS summary_sns_subscription_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

UPDATE dashboard_access
SET email_normalized = COALESCE(email_normalized, LOWER(TRIM(email))),
    role = COALESCE(role, 'viewer'),
    status = COALESCE(status, 'active'),
    is_bootstrap_admin = COALESCE(is_bootstrap_admin, FALSE),
    created_at = COALESCE(created_at, NOW()),
    updated_at = COALESCE(updated_at, NOW());

CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_access_email_normalized_unique
    ON dashboard_access (email_normalized);

CREATE INDEX IF NOT EXISTS idx_dashboard_access_role
    ON dashboard_access (role);
CREATE INDEX IF NOT EXISTS idx_dashboard_access_status
    ON dashboard_access (status);
CREATE INDEX IF NOT EXISTS idx_dashboard_access_cognito_sub
    ON dashboard_access (cognito_sub);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _env_int(name, default, *, minimum=1, maximum=None):
    try:
        value = int(os.getenv(name, ""))
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _db_connect_timeout_seconds():
    return _env_int("API_DB_CONNECT_TIMEOUT_SECONDS", 3, minimum=1, maximum=10)


def _db_statement_timeout_ms():
    return _env_int("API_DB_STATEMENT_TIMEOUT_MS", 10_000, minimum=1_000, maximum=25_000)


def _db_lock_timeout_ms():
    return _env_int("API_DB_LOCK_TIMEOUT_MS", 2_000, minimum=500, maximum=10_000)


def _migration_db_statement_timeout_ms():
    return _env_int("MIGRATION_DB_STATEMENT_TIMEOUT_MS", 25_000, minimum=5_000, maximum=25_000)


def _migration_db_lock_timeout_ms():
    return _env_int("MIGRATION_DB_LOCK_TIMEOUT_MS", 10_000, minimum=1_000, maximum=20_000)


def _aws_connect_timeout_seconds():
    return _env_int("API_AWS_CONNECT_TIMEOUT_SECONDS", 2, minimum=1, maximum=10)


def _aws_read_timeout_seconds():
    return _env_int("API_AWS_READ_TIMEOUT_SECONDS", 5, minimum=1, maximum=20)


def _db_session_settings():
    return [
        ("statement_timeout", _db_statement_timeout_ms()),
        ("lock_timeout", _db_lock_timeout_ms()),
        ("idle_in_transaction_session_timeout", 15_000),
    ]


def _migration_db_session_settings():
    return [
        ("statement_timeout", _migration_db_statement_timeout_ms()),
        ("lock_timeout", _migration_db_lock_timeout_ms()),
        ("idle_in_transaction_session_timeout", 25_000),
    ]


def _configure_db_session(conn, settings):
    # RDS Proxy rejects libpq startup command-line options; apply GUCs after connect.
    with conn.cursor() as cur:
        for name, value in settings:
            cur.execute(f"SET SESSION {name} = %s", (value,))
    conn.commit()


def _aws_client_config():
    return Config(
        connect_timeout=_aws_connect_timeout_seconds(),
        read_timeout=_aws_read_timeout_seconds(),
        retries={"max_attempts": 2, "mode": "standard"},
    )


def _aws_client(service_name):
    return boto3.client(
        service_name,
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        config=_aws_client_config(),
    )


def _load_db_credentials():
    global _db_credentials
    if _db_credentials is not None:
        return _db_credentials

    secret_arn = os.getenv("DB_CREDENTIALS_SECRET_ARN")
    if not secret_arn:
        raise DBSecretConfigError("DB_CREDENTIALS_SECRET_ARN is not set")

    response = _aws_client("secretsmanager").get_secret_value(SecretId=secret_arn)
    raw_secret = response.get("SecretString")
    if not raw_secret:
        raise DBSecretConfigError("DB credentials secret is missing SecretString")

    try:
        payload = json.loads(raw_secret)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DBSecretConfigError("DB credentials secret contains invalid JSON") from exc
    username = payload.get("username")
    password = payload.get("password")
    if not username or not password:
        raise DBSecretConfigError("DB credentials secret must contain username and password")

    _db_credentials = {"username": username, "password": password}
    return _db_credentials


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        credentials = _load_db_credentials()
        conn = psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ["DB_PORT"]),
            dbname=os.environ["DB_NAME"],
            user=credentials["username"],
            password=credentials["password"],
            sslmode="require",
            connect_timeout=_db_connect_timeout_seconds(),
            application_name="fraud-detector-api",
        )
        try:
            _configure_db_session(conn, _db_session_settings())
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        _conn = conn
    return _conn


def _get_migration_conn():
    credentials = _load_db_credentials()
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=credentials["username"],
        password=credentials["password"],
        sslmode="require",
        connect_timeout=_db_connect_timeout_seconds(),
        application_name="fraud-detector-api-migration",
    )
    try:
        _configure_db_session(conn, _migration_db_session_settings())
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def _close_conn():
    global _conn
    conn = _conn
    _conn = None
    if conn is None or getattr(conn, "closed", True):
        return
    try:
        conn.close()
    except Exception:
        pass


def _migrate_database_schema():
    started = perf_counter()
    conn = None
    try:
        conn = _get_migration_conn()
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(
                """
                INSERT INTO schema_migrations (id, applied_at)
                VALUES (%s, NOW())
                ON CONFLICT (id) DO UPDATE SET applied_at = EXCLUDED.applied_at
                """,
                (MIGRATION_ID,),
            )
        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            conn.close()

    duration_ms = round((perf_counter() - started) * 1000)
    _safe_log("db_migration_completed", migration_id=MIGRATION_ID, duration_ms=duration_ms)
    return _ok({"status": "migrated", "migration_id": MIGRATION_ID, "duration_ms": duration_ms})

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ok(data, meta=None):
    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return {"statusCode": 200, "headers": HEADERS, "body": json.dumps(body, default=str)}


def _err(status, code, message):
    return {
        "statusCode": status,
        "headers": HEADERS,
        "body": json.dumps({"error": {"code": code, "message": message}}),
    }


def _preflight():
    return {"statusCode": 204, "headers": HEADERS, "body": ""}


def _headers(event):
    raw_headers = event.get("headers") or {}
    return {str(key).lower(): value for key, value in raw_headers.items() if value is not None}


def _with_trace(response, trace_id):
    headers = dict(response.get("headers") or {})
    headers["X-Trace-Id"] = trace_id
    response["headers"] = headers
    return response


def _request_trace_id(event):
    header_trace_id = str(_headers(event).get("x-trace-id") or "").strip()
    if _is_safe_trace_id(header_trace_id):
        return header_trace_id[:128]
    return str(uuid.uuid4())


def _request_id(event, context):
    request_id = ((event.get("requestContext") or {}).get("requestId") or "")
    if request_id:
        return request_id
    return getattr(context, "aws_request_id", "") if context is not None else ""


def _route_template(path, path_params):
    if path in [
      "/health",
      "/dashboard/me",
      "/dashboard/me/password",
      "/dashboard/invites",
      "/stats",
      "/stats/timeseries",
      "/filters",
      "/transactions",
      "/users"
    ] :
        return path
    if path.startswith("/dashboard/invites/") and path_params.get("id"):
        return "/dashboard/invites/{id}"
    if path.startswith("/transactions/") and path_params.get("id"):
        return "/transactions/{id}"
    if path.startswith("/users/") and path.endswith("/behavior"):
        return "/users/{id}/behavior"
    if path.startswith("/users/") and path_params.get("id"):
        return "/users/{id}"
    return "unknown"


def _response_row_count(response):
    try:
        body = response.get("body")
        if not body:
            return None
        parsed = json.loads(body)
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict) and "recent_transactions" in data and isinstance(data["recent_transactions"], list):
            return len(data["recent_transactions"])
    except Exception:
        return None
    return None


def _safe_log(action, **fields):
    record = {
        "level": fields.pop("level", "INFO"),
        "component": "api",
        "action": action,
    }
    for key, value in fields.items():
        if value is None or _is_sensitive_log_key(key):
            continue
        record[key] = value
    logger.info(json.dumps(record, default=str))


safe_log = _safe_log


def _is_sensitive_log_key(key):
    return key in {
        "transaction_id",
        "user_id",
        "amount",
        "currency",
        "country",
        "channel",
        "destination_account",
        "fraud_score",
        "decision",
        "payload",
        "body",
        "query",
        "email",
        "auth_claims",
    }


def _error_class(exc):
    return exc.__class__.__name__


def _is_safe_trace_id(value):
    value = str(value or "").strip()
    if not value or len(value) > 128:
        return False
    if value.startswith("legacy-"):
        suffix = value[len("legacy-") :]
        return len(suffix) == 32 and all(char in "0123456789abcdef" for char in suffix)
    if value.startswith("sqs-"):
        suffix = value[len("sqs-") :]
        return len(suffix) == 32 and all(char in "0123456789abcdef" for char in suffix)
    try:
        uuid.UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def _is_truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _normalize_email(email):
    return email.strip().lower()


def _serialize_row(row):
    out = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, (uuid.UUID,)):
            out[key] = str(value)
        elif isinstance(value, (decimal.Decimal,)):
            out[key] = float(value)
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


def _get_dynamodb_client():
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = _aws_client("dynamodb")
    return _dynamodb_client


def _user_behavior_table_name():
    return (os.getenv("USER_BEHAVIOR_TABLE_NAME") or os.getenv("DYNAMODB_TABLE_NAME") or "").strip()


def _dynamodb_value(value):
    if not isinstance(value, dict):
        return value
    if "S" in value:
        return value["S"]
    if "N" in value:
        raw = value["N"]
        try:
            parsed = decimal.Decimal(raw)
        except decimal.InvalidOperation:
            return raw
        if parsed == parsed.to_integral_value():
            return int(parsed)
        return float(parsed)
    if "BOOL" in value:
        return bool(value["BOOL"])
    if "NULL" in value:
        return None
    if "L" in value:
        return [_dynamodb_value(item) for item in value["L"]]
    if "M" in value:
        return {key: _dynamodb_value(item) for key, item in value["M"].items()}
    return None


def _deserialize_dynamodb_item(item):
    return {key: _dynamodb_value(value) for key, value in (item or {}).items()}


def _as_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_string_list(value):
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _empty_user_behavior_profile(user_id):
    return {
        "user_id": user_id,
        "has_profile": False,
        "avg_amount": None,
        "std_dev_amount": None,
        "tx_count": 0,
        "tx_last_hour": 0,
        "tx_last_10min": 0,
        "typical_countries": [],
        "typical_channels": [],
        "known_destinations": [],
        "last_country": "",
        "last_timestamp": None,
    }


def _serialize_user_behavior_profile(user_id, item):
    if not item:
        return _empty_user_behavior_profile(user_id)

    profile = _deserialize_dynamodb_item(item)
    return {
        "user_id": str(profile.get("user_id") or user_id),
        "has_profile": True,
        "avg_amount": _as_float(profile.get("avg_amount")),
        "std_dev_amount": _as_float(profile.get("std_dev_amount")),
        "tx_count": _as_int(profile.get("tx_count")),
        "tx_last_hour": _as_int(profile.get("tx_last_hour")),
        "tx_last_10min": _as_int(profile.get("tx_last_10min")),
        "typical_countries": _as_string_list(profile.get("typical_countries")),
        "typical_channels": _as_string_list(profile.get("typical_channels")),
        "known_destinations": _as_string_list(profile.get("known_destinations")),
        "last_country": str(profile.get("last_country") or ""),
        "last_timestamp": profile.get("last_timestamp"),
    }


def _path_user_id(path, path_params, *, suffix=""):
    if path_params.get("id"):
        return str(path_params["id"])
    if not path.startswith("/users/"):
        return ""
    body = path[len("/users/") :]
    if suffix:
        if not body.endswith(suffix):
            return ""
        body = body[: -len(suffix)]
    return unquote(body).strip()


def _json_body(event):
    raw_body = event.get("body")
    if raw_body in (None, ""):
        return {}
    if isinstance(raw_body, dict):
        return raw_body
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError:
        return None


def _parse_int(value, default, minimum=None, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(parsed, minimum)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _is_date_only(value):
    return isinstance(value, str) and _DATE_ONLY_RE.match(value) is not None


def _append_time_filter(filters, params, column, value, *, end=False):
    if _is_date_only(value):
        if end:
            filters.append(f"{column} < (%s::date + INTERVAL '1 day')")
        else:
            filters.append(f"{column} >= %s::date")
        params.append(value)
        return

    filters.append(f"{column} <= %s" if end else f"{column} >= %s")
    params.append(value)


def _build_where(query, extra_filters=None, extra_params=None):
    """Build (filters, params) from shared query-string filter params."""
    filters = list(extra_filters or [])
    params = list(extra_params or [])

    if query.get("user_id"):
        filters.append("user_id = %s")
        params.append(query["user_id"])
    trace_id = str(query.get("trace_id") or "").strip()
    if _is_safe_trace_id(trace_id):
        filters.append("trace_id = %s")
        params.append(trace_id[:128])
    if query.get("country"):
        filters.append("country = %s")
        params.append(query["country"])
    if query.get("channel"):
        filters.append("channel = %s")
        params.append(query["channel"])
    if query.get("is_fraud") in ("true", "1"):
        filters.append("is_fraud = TRUE")
    elif query.get("is_fraud") in ("false", "0"):
        filters.append("is_fraud = FALSE")
    if query.get("from"):
        _append_time_filter(filters, params, "processed_at", query["from"])
    if query.get("to"):
        _append_time_filter(filters, params, "processed_at", query["to"], end=True)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    return where, params


def _sort_clause(query, sortable_columns, default_column):
    sort_by = query.get("sort_by")
    sort_col = sortable_columns.get(sort_by, sortable_columns[default_column])

    sort_order = str(query.get("sort_order", "desc")).lower()
    sort_dir = "ASC" if sort_order == "asc" else "DESC"

    return sort_col, sort_dir


def _request_identity(event):
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    bypass = False

    if not claims and _is_truthy(os.getenv("AUTH_LOCAL_BYPASS")):
        bypass = True
        headers = _headers(event)
        claims = {
            "email": headers.get("x-auth-email") or headers.get("x-cognito-email") or headers.get("email"),
            "email_verified": headers.get("x-auth-email-verified", "true"),
            "sub": headers.get("x-auth-sub") or headers.get("x-cognito-sub"),
            "identities": headers.get("x-auth-identities") or "",
        }

    email = claims.get("email")
    if not email:
        return None, _err(401, "EMAIL_REQUIRED", "An email claim is required")
    if not _is_truthy(claims.get("email_verified")):
        return None, _err(403, "EMAIL_NOT_VERIFIED", "The email claim must be verified")

    return {
        "email": email,
        "email_normalized": _normalize_email(email),
        "cognito_sub": claims.get("sub"),
        "identities": claims.get("identities") or "",
        "bypass": bypass,
    }, None


def _auth_provider(access, identity):
    if identity.get("bypass"):
        return "local-bypass"
    identities = identity.get("identities")
    if isinstance(identities, str) and "Google" in identities:
        return "google"
    if isinstance(identities, list) and any(item.get("providerName") == "Google" for item in identities if isinstance(item, dict)):
        return "google"
    if access.get("cognito_sub"):
        return "cognito"
    return "cognito"


def _get_access_row(conn, *, email_normalized):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM dashboard_access
            WHERE email_normalized = %s
            FOR UPDATE
            """,
            (email_normalized,),
        )
        return cur.fetchone()


def _touch_access_row(cur, access_id, cognito_sub=None):
    cur.execute(
        """
        UPDATE dashboard_access
        SET cognito_sub = COALESCE(%s, cognito_sub),
            last_login_at = NOW(),
            updated_at = NOW()
        WHERE id = %s
        RETURNING *
        """,
        (cognito_sub, access_id),
    )
    return cur.fetchone()


def _summary_topic_arn():
    return os.getenv("SUMMARY_SNS_TOPIC_ARN") or ""


def _subscribe_summary_email(email):
    topic_arn = _summary_topic_arn()
    if not topic_arn:
        raise RuntimeError("SUMMARY_SNS_TOPIC_ARN is not configured")

    client = _aws_client("sns")
    existing_arn = _find_summary_subscription_arn(client, topic_arn, email)
    if existing_arn:
        return existing_arn

    response = client.subscribe(
        TopicArn=topic_arn,
        Protocol="email",
        Endpoint=email,
        ReturnSubscriptionArn=True,
    )
    return response.get("SubscriptionArn")


def _find_summary_subscription_arn(client, topic_arn, email):
    paginator = client.get_paginator("list_subscriptions_by_topic")
    email_normalized = _normalize_email(email)
    for page in paginator.paginate(TopicArn=topic_arn):
        for subscription in page.get("Subscriptions", []):
            if _normalize_email(subscription.get("Endpoint", "")) == email_normalized:
                return subscription.get("SubscriptionArn")
    return None


def _unsubscribe_summary_email(subscription_arn):
    if not subscription_arn:
        return
    client = _aws_client("sns")
    client.unsubscribe(SubscriptionArn=subscription_arn)


def _set_summary_subscription(cur, access_id, *, subscription_arn=None, status, warning=None, clear_arn=False):
    if clear_arn:
        cur.execute(
            """
            UPDATE dashboard_access
            SET summary_sns_subscription_arn = NULL,
                summary_sns_subscription_status = %s,
                summary_sns_subscription_warning = %s,
                summary_sns_subscription_updated_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (status, warning, access_id),
        )
    else:
        cur.execute(
            """
            UPDATE dashboard_access
            SET summary_sns_subscription_arn = COALESCE(%s, summary_sns_subscription_arn),
                summary_sns_subscription_status = %s,
                summary_sns_subscription_warning = %s,
                summary_sns_subscription_updated_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (subscription_arn, status, warning, access_id),
        )
    return cur.fetchone()


def _try_subscribe_summary_for_access(cur, row):
    if row.get("summary_sns_subscription_status") == "pending_confirmation":
        return row
    if row.get("summary_sns_subscription_arn"):
        return row

    try:
        subscription_arn = _subscribe_summary_email(row["email"])
    except Exception as exc:
        _safe_log("summary_sns_subscribe_failed", level="WARN", error_class=_error_class(exc))
        return _set_summary_subscription(
            cur,
            row["id"],
            status="subscribe_failed",
            warning=str(exc)[:500],
        )

    return _set_summary_subscription(
        cur,
        row["id"],
        subscription_arn=subscription_arn,
        status="pending_confirmation",
    )


def _try_subscribe_summary_standalone(email):
    try:
        subscription_arn = _subscribe_summary_email(email)
    except Exception as exc:
        _safe_log("summary_sns_bootstrap_subscribe_failed", level="WARN", error_class=_error_class(exc))
        return {
            "email": email,
            "status": "subscribe_failed",
            "warning": str(exc)[:500],
        }

    return {
        "email": email,
        "subscription_arn": subscription_arn,
        "status": "pending_confirmation",
    }


def _authorize_access(event, *, activate_pending=False, touch_login=False):
    identity, error = _request_identity(event)
    if error is not None:
        return None, error

    conn = _get_conn()

    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM dashboard_access
                WHERE email_normalized = %s
                """,
                (identity["email_normalized"],),
            )
            row = cur.fetchone()
            if row is None:
                return None, _err(403, "DASHBOARD_ACCESS_NOT_INVITED", "This account is not invited")
            if row["status"] == "disabled":
                return None, _err(403, "DASHBOARD_ACCESS_DISABLED", "This account is disabled")

            if row["status"] == "pending":
                if not activate_pending:
                    return None, _err(403, "DASHBOARD_ACCESS_PENDING", "This account is still pending activation")
                cur.execute(
                    """
                    UPDATE dashboard_access
                    SET status = 'active',
                        activated_at = COALESCE(activated_at, NOW()),
                        cognito_sub = COALESCE(%s, cognito_sub),
                        last_login_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (identity["cognito_sub"], row["id"]),
                )
                row = cur.fetchone()
                row = _try_subscribe_summary_for_access(cur, row)
            elif touch_login:
                row = _touch_access_row(cur, row["id"], identity["cognito_sub"])

    return {"identity": identity, "access": row}, None


def _serialize_access_profile(access, identity):
    auth_provider = _auth_provider(access, identity)
    return {
        "email": access["email"],
        "role": access["role"],
        "status": access["status"],
        "can_manage_invites": access["role"] == "admin",
        "can_change_password": bool(access.get("cognito_sub")) and auth_provider == "cognito",
        "auth_provider": auth_provider,
        "is_bootstrap_admin": access["is_bootstrap_admin"],
    }


def _require_admin(access):
    if access["role"] != "admin":
        return _err(403, "DASHBOARD_ADMIN_REQUIRED", "Admin access required")
    return None


def _bootstrap_dashboard_admin(event):
    payload = event.get("payload") or event.get("detail") or event
    email = payload.get("email")
    if not email:
        return _err(400, "EMAIL_REQUIRED", "Bootstrap admin email is required")

    email_normalized = _normalize_email(email)
    display_name = payload.get("display_name")
    cognito_sub = payload.get("cognito_sub")
    alert_email = payload.get("alert_email")
    alert_email_normalized = _normalize_email(alert_email) if alert_email else ""

    conn = _get_conn()

    standalone_subscription = None
    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT email_normalized
                FROM dashboard_access
                WHERE is_bootstrap_admin = TRUE
                  AND email_normalized <> %s
                LIMIT 1
                """,
                (email_normalized,),
            )
            existing_bootstrap = cur.fetchone()
            if existing_bootstrap is not None:
                return _err(
                    409,
                    "BOOTSTRAP_ADMIN_EXISTS",
                    "A different bootstrap admin already exists",
                )

            cur.execute(
                """
                INSERT INTO dashboard_access (
                    email,
                    email_normalized,
                    display_name,
                    role,
                    status,
                    cognito_sub,
                    invited_by_email,
                    is_bootstrap_admin,
                    invited_at,
                    activated_at,
                    disabled_at,
                    last_login_at,
                    summary_sns_subscription_arn,
                    summary_sns_subscription_status,
                    summary_sns_subscription_warning,
                    summary_sns_subscription_updated_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, 'admin', 'active', %s, NULL, TRUE,
                    NOW(), NOW(), NULL, NOW(), NULL, NULL, NULL, NULL, NOW(), NOW()
                )
                ON CONFLICT (email_normalized) DO UPDATE SET
                    email = EXCLUDED.email,
                    display_name = COALESCE(EXCLUDED.display_name, dashboard_access.display_name),
                    role = 'admin',
                    status = 'active',
                    cognito_sub = COALESCE(EXCLUDED.cognito_sub, dashboard_access.cognito_sub),
                    invited_by_email = dashboard_access.invited_by_email,
                    is_bootstrap_admin = TRUE,
                    invited_at = COALESCE(dashboard_access.invited_at, EXCLUDED.invited_at),
                    activated_at = COALESCE(dashboard_access.activated_at, EXCLUDED.activated_at),
                    disabled_at = NULL,
                    last_login_at = EXCLUDED.last_login_at,
                    updated_at = NOW()
                RETURNING *
                """,
                (email, email_normalized, display_name, cognito_sub),
            )
            row = cur.fetchone()

            if alert_email_normalized and alert_email_normalized == email_normalized:
                row = _try_subscribe_summary_for_access(cur, row)
            elif alert_email_normalized:
                standalone_subscription = _try_subscribe_summary_standalone(alert_email_normalized)

    data = _serialize_row(row)
    if standalone_subscription is not None:
        data["bootstrap_alert_subscription"] = standalone_subscription
    return _ok(data)


def _health():
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return _ok({"status": "ok"})
    except Exception as exc:
        _close_conn()
        _safe_log("api_health_failed", level="ERROR", error_class=_error_class(exc))
        return _err(503, "SERVICE_UNAVAILABLE", "Database unreachable")


def _get_stats(query):
    conn = _get_conn()
    where, params = _build_where(query)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*)                                        AS total,
                COUNT(*) FILTER (WHERE is_fraud = TRUE)        AS fraud,
                COUNT(*) FILTER (WHERE decision = 'allow')     AS allowed,
                COUNT(*) FILTER (WHERE decision = 'block')     AS blocked,
                COUNT(*) FILTER (WHERE decision = 'challenge') AS challenged,
                ROUND(AVG(fraud_score)::numeric, 4)            AS avg_fraud_score
            FROM transactions
            {where}
            """,
            params,
        )
        row = cur.fetchone()
    total = row[0] or 0
    fraud = row[1] or 0
    return _ok(
        {
            "total": total,
            "fraud": fraud,
            "allowed": row[2] or 0,
            "blocked": row[3] or 0,
            "challenged": row[4] or 0,
            "fraud_rate": round(fraud / total, 4) if total > 0 else 0.0,
            "avg_fraud_score": float(row[5]) if row[5] is not None else None,
        }
    )


def _get_stats_timeseries(query):
    conn = _get_conn()

    granularity = query.get("granularity", "hour")
    if granularity not in ("second", "minute", "hour", "day"):
        granularity = "hour"

    if granularity == "second":
        seconds = _parse_int(query.get("seconds"), 60, minimum=1, maximum=3600)
        time_clause = f"processed_at >= NOW() - INTERVAL '{seconds} seconds'"
    elif granularity == "minute":
        minutes = _parse_int(query.get("minutes"), 60, minimum=1, maximum=1440)
        time_clause = f"processed_at >= NOW() - INTERVAL '{minutes} minutes'"
    else:
        days = _parse_int(query.get("days"), 1, minimum=1, maximum=90)
        time_clause = f"processed_at >= NOW() - INTERVAL '{days} days'"

    where, params = _build_where(query)
    full_where = (where + f" AND {time_clause}") if where else f"WHERE {time_clause}"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                date_trunc('{granularity}', processed_at) AS hour,
                COUNT(*)                                   AS total,
                COUNT(*) FILTER (WHERE is_fraud)           AS fraud
            FROM transactions
            {full_where}
            GROUP BY 1
            ORDER BY 1
            """,
            params,
        )
        rows = [_serialize_row(r) for r in cur.fetchall()]
    return _ok(rows)


def _get_filters():
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT country FROM transactions WHERE country IS NOT NULL ORDER BY country")
        countries = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT channel FROM transactions WHERE channel IS NOT NULL ORDER BY channel")
        channels = [r[0] for r in cur.fetchall()]
    return _ok({"countries": countries, "channels": channels})


def _list_transactions(query):
    conn = _get_conn()

    limit = _parse_int(query.get("limit"), 20, minimum=1, maximum=100)
    offset = _parse_int(query.get("offset"), 0, minimum=0)

    where, params = _build_where(query)

    sort_col, sort_dir = _sort_clause(query, _SORTABLE_TX, "processed_at")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS cnt FROM transactions {where}", params)
        total = cur.fetchone()["cnt"]

        cur.execute(
            f"""
            SELECT transaction_id, user_id, amount, currency, country, channel,
                   fraud_score, is_fraud, decision, processed_at, trace_id, ingested_at
            FROM   transactions
            {where}
            ORDER  BY {sort_col} {sort_dir} NULLS LAST
            LIMIT  %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = [_serialize_row(r) for r in cur.fetchall()]

    return _ok(
        rows,
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort_by": sort_col,
            "sort_order": sort_dir.lower(),
        },
    )


def _get_transaction(tx_id):
    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT transaction_id, user_id, amount, currency, country, channel,
                   fraud_score, is_fraud, decision, processed_at, trace_id, ingested_at
            FROM   transactions WHERE transaction_id = %s
            """,
            (tx_id,),
        )
        row = cur.fetchone()
    if row is None:
        return _err(404, "NOT_FOUND", f"Transaction '{tx_id}' not found")
    return _ok(_serialize_row(row))


def _list_users(query):
    conn = _get_conn()

    limit = _parse_int(query.get("limit"), 20, minimum=1, maximum=100)
    offset = _parse_int(query.get("offset"), 0, minimum=0)

    sub_filters = []
    sub_params = []
    if query.get("country"):
        sub_filters.append("country = %s")
        sub_params.append(query["country"])
    if query.get("channel"):
        sub_filters.append("channel = %s")
        sub_params.append(query["channel"])
    if query.get("from"):
        _append_time_filter(sub_filters, sub_params, "processed_at", query["from"])
    if query.get("to"):
        _append_time_filter(sub_filters, sub_params, "processed_at", query["to"], end=True)
    if query.get("user_id"):
        sub_filters.append("user_id = %s")
        sub_params.append(query["user_id"])

    sub_where = ("WHERE " + " AND ".join(sub_filters)) if sub_filters else ""

    sort_col, sort_dir = _sort_clause(query, _SORTABLE_USERS, "fraud_count")

    count_sql = f"""
        SELECT COUNT(DISTINCT user_id) AS cnt
        FROM transactions {sub_where}
    """
    list_sql = f"""
        WITH stats AS (
            SELECT
                user_id,
                COUNT(*)                                            AS total_transactions,
                COUNT(*) FILTER (WHERE is_fraud)                   AS fraud_count,
                ROUND(AVG(fraud_score)::numeric, 4)                AS avg_fraud_score,
                MAX(processed_at)                                   AS last_seen,
                ROUND(
                    CAST(COUNT(*) FILTER (WHERE is_fraud) AS numeric)
                    / NULLIF(COUNT(*), 0) * 100, 1
                )                                                   AS fraud_rate_pct
            FROM transactions
            {sub_where}
            GROUP BY user_id
        )
        SELECT * FROM stats
        ORDER BY {sort_col} {sort_dir} NULLS LAST
        LIMIT %s OFFSET %s
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(count_sql, sub_params)
        total = cur.fetchone()["cnt"]
        cur.execute(list_sql, sub_params + [limit, offset])
        rows = [_serialize_row(r) for r in cur.fetchall()]

    return _ok(
        rows,
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort_by": sort_col,
            "sort_order": sort_dir.lower(),
        },
    )


def _get_user(user_id):
    conn = _get_conn()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT user_id,
                   COUNT(*)                                AS total_transactions,
                   COUNT(*) FILTER (WHERE is_fraud)        AS fraud_count,
                   ROUND(AVG(fraud_score)::numeric, 4)     AS avg_fraud_score,
                   MAX(processed_at)                       AS last_seen
            FROM transactions WHERE user_id = %s GROUP BY user_id
            """,
            (user_id,),
        )
        row = cur.fetchone()

    if row is None:
        return _err(404, "NOT_FOUND", f"User '{user_id}' not found")

    summary = _serialize_row(row)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT transaction_id, amount, currency, country, channel,
                   fraud_score, is_fraud, decision, processed_at, trace_id, ingested_at
            FROM   transactions WHERE user_id = %s ORDER BY processed_at DESC LIMIT 10
            """,
            (user_id,),
        )
        summary["recent_transactions"] = [_serialize_row(r) for r in cur.fetchall()]

    return _ok(summary)


def _get_user_behavior(user_id):
    table_name = _user_behavior_table_name()
    if not table_name:
        return _err(503, "BEHAVIOR_TABLE_NOT_CONFIGURED", "User behavior table is not configured")

    try:
        response = _get_dynamodb_client().get_item(
            TableName=table_name,
            Key={"user_id": {"S": user_id}},
            ConsistentRead=False,
        )
    except ClientError as exc:
        _safe_log("user_behavior_load_failed", level="WARN", error_class=_error_class(exc))
        return _err(502, "BEHAVIOR_PROFILE_UNAVAILABLE", "User behavior profile is unavailable")

    return _ok(_serialize_user_behavior_profile(user_id, response.get("Item")))


def _list_dashboard_invites(access):
    admin_error = _require_admin(access)
    if admin_error is not None:
        return admin_error

    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM dashboard_access
            ORDER BY created_at DESC, email_normalized ASC
            """
        )
        rows = [_serialize_row(r) for r in cur.fetchall()]
    return _ok(rows, {"total": len(rows)})


def _upsert_dashboard_invite(access, body):
    admin_error = _require_admin(access)
    if admin_error is not None:
        return admin_error

    email = body.get("email")
    if not email:
        return _err(400, "EMAIL_REQUIRED", "Invite email is required")

    email_normalized = _normalize_email(email)
    display_name = body.get("display_name")

    conn = _get_conn()

    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM dashboard_access
                WHERE email_normalized = %s
                FOR UPDATE
                """,
                (email_normalized,),
            )
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    """
                    INSERT INTO dashboard_access (
                        email,
                        email_normalized,
                        display_name,
                        role,
                        status,
                        cognito_sub,
                        invited_by_email,
                        is_bootstrap_admin,
                        invited_at,
                        activated_at,
                        disabled_at,
                        last_login_at,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        %s, %s, %s, 'viewer', 'pending', NULL, %s, FALSE,
                        NOW(), NULL, NULL, NULL, NOW(), NOW()
                    )
                    RETURNING *
                    """,
                    (email, email_normalized, display_name, access["email"]),
                )
                row = cur.fetchone()
            elif row["status"] == "disabled":
                cur.execute(
                    """
                    UPDATE dashboard_access
                    SET email = %s,
                        display_name = COALESCE(%s, display_name),
                        role = 'viewer',
                        status = 'pending',
                        cognito_sub = NULL,
                        invited_by_email = %s,
                        invited_at = NOW(),
                        activated_at = NULL,
                        disabled_at = NULL,
                        last_login_at = NULL,
                        summary_sns_subscription_arn = NULL,
                        summary_sns_subscription_status = NULL,
                        summary_sns_subscription_warning = NULL,
                        summary_sns_subscription_updated_at = NULL,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (email, display_name, access["email"], row["id"]),
                )
                row = cur.fetchone()
            elif row["status"] == "pending":
                cur.execute(
                    """
                    UPDATE dashboard_access
                    SET email = %s,
                        display_name = COALESCE(%s, display_name),
                        role = 'viewer',
                        status = 'pending',
                        invited_by_email = %s,
                        invited_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (email, display_name, access["email"], row["id"]),
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    """
                    UPDATE dashboard_access
                    SET email = %s,
                        display_name = COALESCE(%s, display_name),
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (email, display_name, row["id"]),
                )
                row = cur.fetchone()

    return _ok(_serialize_row(row))


def _delete_dashboard_invite(access, invite_id):
    admin_error = _require_admin(access)
    if admin_error is not None:
        return admin_error

    conn = _get_conn()

    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM dashboard_access
                WHERE id = %s
                FOR UPDATE
                """,
                (invite_id,),
            )
            row = cur.fetchone()
            if row is None:
                return _err(404, "NOT_FOUND", f"Invite '{invite_id}' not found")
            if row["is_bootstrap_admin"]:
                return _err(403, "CANNOT_DISABLE_BOOTSTRAP_ADMIN", "The bootstrap admin cannot be disabled")

            cur.execute(
                """
                UPDATE dashboard_access
                SET status = 'disabled',
                    disabled_at = COALESCE(disabled_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (invite_id,),
            )
            row = cur.fetchone()

            if row.get("summary_sns_subscription_arn"):
                try:
                    _unsubscribe_summary_email(row["summary_sns_subscription_arn"])
                except Exception as exc:
                    _safe_log("summary_sns_unsubscribe_failed", level="WARN", error_class=_error_class(exc))
                    row = _set_summary_subscription(
                        cur,
                        row["id"],
                        status="unsubscribe_failed",
                        warning=str(exc)[:500],
                    )
                else:
                    row = _set_summary_subscription(
                        cur,
                        row["id"],
                        status="unsubscribed",
                        clear_arn=True,
                    )

    return _ok(_serialize_row(row))


def _get_dashboard_me(access, identity):
    return _ok(_serialize_access_profile(access, identity))


def _change_dashboard_password(access, event):
    body = _json_body(event)
    if body is None:
        return _err(400, "INVALID_JSON", "Request body must be valid JSON")

    current_password = body.get("current_password")
    new_password = body.get("new_password")
    new_password_confirmation = body.get("new_password_confirmation")

    if not current_password or not new_password or not new_password_confirmation:
        return _err(
            400,
            "VALIDATION_ERROR",
            "current_password, new_password, and new_password_confirmation are required",
        )
    if new_password != new_password_confirmation:
        return _err(400, "VALIDATION_ERROR", "Password confirmation does not match")
    if not access.get("cognito_sub"):
        return _err(403, "PASSWORD_CHANGE_UNAVAILABLE", "Password changes require a Cognito account")

    headers = _headers(event)
    access_token = headers.get("x-cognito-access-token")
    if not access_token:
        return _err(401, "ACCESS_TOKEN_REQUIRED", "X-Cognito-Access-Token is required")

    client = _aws_client("cognito-idp")

    try:
        cognito_user = client.get_user(AccessToken=access_token)
        token_sub = None
        for attr in cognito_user.get("UserAttributes", []):
            if attr.get("Name") == "sub":
                token_sub = attr.get("Value")
                break
        if token_sub and access.get("cognito_sub") and token_sub != access.get("cognito_sub"):
            return _err(403, "TOKEN_SUB_MISMATCH", "The access token does not match the current dashboard user")
        client.change_password(
            AccessToken=access_token,
            PreviousPassword=current_password,
            ProposedPassword=new_password,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "ClientError")
        if error_code == "NotAuthorizedException":
            return _err(403, "INVALID_CURRENT_PASSWORD", "The current password is invalid")
        if error_code == "InvalidPasswordException":
            return _err(400, "WEAK_PASSWORD", "The new password does not satisfy the password policy")
        if error_code == "LimitExceededException":
            return _err(429, "PASSWORD_CHANGE_THROTTLED", "Password changes are temporarily throttled")
        if error_code == "InvalidParameterException":
            return _err(400, "INVALID_PASSWORD_REQUEST", "The password change request is invalid")
        return _err(502, "AUTH_PROVIDER_ERROR", "Password change failed")

    return _ok({"status": "changed"})


# ── Router ────────────────────────────────────────────────────────────────────

def _dispatch_request(event, path, query, path_params, method):
    if method == "OPTIONS":
        return _preflight()

    if path == "/health":
        return _health()

    protected_access = None
    protected_identity = None

    if path != "/health":
        auth_result, auth_error = _authorize_access(
            event,
            activate_pending=(path == "/dashboard/me"),
            touch_login=(path == "/dashboard/me"),
        )
        if auth_error is not None:
            return auth_error
        protected_identity = auth_result["identity"]
        protected_access = auth_result["access"]

    if path == "/dashboard/me":
        return _get_dashboard_me(protected_access, protected_identity)
    if path == "/dashboard/me/password" and method == "PUT":
        return _change_dashboard_password(protected_access, event)
    if path == "/dashboard/invites" and method == "GET":
        return _list_dashboard_invites(protected_access)
    if path == "/dashboard/invites" and method == "POST":
        body = _json_body(event)
        if body is None:
            return _err(400, "INVALID_JSON", "Request body must be valid JSON")
        return _upsert_dashboard_invite(protected_access, body)
    if path.startswith("/dashboard/invites/") and path_params.get("id") and method == "DELETE":
        return _delete_dashboard_invite(protected_access, path_params["id"])

    if path == "/stats":
        return _get_stats(query)
    if path == "/stats/timeseries":
        return _get_stats_timeseries(query)
    if path == "/filters":
        return _get_filters()
    if path == "/transactions":
        return _list_transactions(query)
    if path_params.get("id") and path.startswith("/transactions/"):
        return _get_transaction(path_params["id"])
    if path == "/users":
        return _list_users(query)
    if path.startswith("/users/") and path.endswith("/behavior") and method == "GET":
        user_id = _path_user_id(path, path_params, suffix="/behavior")
        if not user_id:
            return _err(400, "USER_ID_REQUIRED", "User id is required")
        return _get_user_behavior(user_id)
    if path_params.get("id") and path.startswith("/users/"):
        return _get_user(path_params["id"])

    return _err(404, "NOT_FOUND", "Route not found")


def handler(event, context):
    started = perf_counter()
    trace_id = _request_trace_id(event)
    request_id = _request_id(event, context)
    path = event.get("rawPath", "")
    query = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    route = _route_template(path, path_params)
    error_class = None
    action = event.get("action")
    is_bootstrap = action == "bootstrap_dashboard_admin"
    is_migration = action == "migrate_database_schema"

    if is_bootstrap or is_migration:
        method = "INVOKE"
        route = action

    _safe_log(
        "api_request_started",
        trace_id=trace_id,
        request_id=request_id,
        method=method,
        route=route,
    )

    try:
        if is_migration:
            response = _migrate_database_schema()
        elif is_bootstrap:
            response = _bootstrap_dashboard_admin(event)
        else:
            response = _dispatch_request(event, path, query, path_params, method)
    except (psycopg2.OperationalError, psycopg2.DatabaseError) as exc:
        error_class = _error_class(exc)
        _close_conn()
        logger.exception("api_database_error")
        response = _err(503, "SERVICE_UNAVAILABLE", "Database temporarily unavailable")
    except (BotoCoreError, ClientError, DBSecretConfigError) as exc:
        error_class = _error_class(exc)
        logger.exception("api_aws_client_error")
        response = _err(502, "UPSTREAM_UNAVAILABLE", "Upstream AWS service temporarily unavailable")
    except Exception as exc:
        error_class = _error_class(exc)
        logger.exception("api_unhandled_exception")
        response = _err(500, "INTERNAL_ERROR", "Internal server error")

    response = _with_trace(response, trace_id)
    _safe_log(
        "api_request_completed",
        trace_id=trace_id,
        request_id=request_id,
        method=method,
        route=route,
        status_code=response.get("statusCode"),
        row_count=_response_row_count(response),
        error_class=error_class,
        duration_ms=round((perf_counter() - started) * 1000),
    )
    return response
