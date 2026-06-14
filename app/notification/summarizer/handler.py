import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sqs = boto3.client("sqs")
sns = boto3.client("sns")
MAX_TRACE_SAMPLES = 20


def _safe_log(action, **fields):
    record = {
        "level": fields.pop("level", "INFO"),
        "component": "fraud_summarizer",
        "action": action,
    }
    for key, value in fields.items():
        if value is None or _is_sensitive_log_key(key):
            continue
        record[key] = value
    logger.info(json.dumps(record, default=str))


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
        "receipt",
        "receipt_handle",
        "email",
    }


def _trace_summary(events):
    seen = set()
    samples = []
    for event in events:
        trace_id = str(event.get("trace_id") or "").strip()
        if not trace_id or trace_id in seen:
            continue
        seen.add(trace_id)
        if len(samples) < MAX_TRACE_SAMPLES:
            samples.append(trace_id)
    return len(seen), samples


def _parse_decimal(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_payload(body):
    payload = json.loads(body)
    if "Message" in payload:
        return json.loads(payload["Message"])
    return payload


def _receive_messages(queue_url, max_messages):
    messages = []
    while len(messages) < max_messages:
        batch_size = min(10, max_messages - len(messages))
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=batch_size,
            WaitTimeSeconds=1,
        )
        batch = response.get("Messages", [])
        if not batch:
            break
        messages.extend(batch)
    return messages


def _delete_messages(queue_url, messages):
    for start in range(0, len(messages), 10):
        entries = [
            {"Id": str(index), "ReceiptHandle": message["ReceiptHandle"]}
            for index, message in enumerate(messages[start : start + 10])
        ]
        if entries:
            sqs.delete_message_batch(QueueUrl=queue_url, Entries=entries)


def _aggregate_events(events):
    deduped = {}
    for event in events:
        tx_id = str(event.get("transaction_id") or "")
        if tx_id:
            deduped[tx_id] = event

    rows = list(deduped.values())
    currencies = defaultdict(Decimal)
    users = Counter()
    countries = Counter()
    channels = Counter()

    for row in rows:
        currency = str(row.get("currency") or "N/A")
        currencies[currency] += _parse_decimal(row.get("amount"))
        users[str(row.get("user_id") or "N/A")] += 1
        countries[str(row.get("country") or "N/A")] += 1
        channels[str(row.get("channel") or "N/A")] += 1

    highest_risk = sorted(rows, key=lambda item: float(item.get("fraud_score") or 0), reverse=True)[:10]
    return rows, currencies, users, countries, channels, highest_risk


def _format_amount(amount):
    return f"{amount:,.2f}"


def _format_score(value):
    score = float(value or 0)
    if score <= 1:
        score *= 100
    return f"{score:.1f}%"


def _build_summary_text(
    rows,
    currencies,
    users,
    countries,
    channels,
    highest_risk,
    *,
    generated_at,
    dashboard_url,
):
    lines = [
        "══════════════════════════════════════",
        "  RESUMEN DE FRAUDE — Dashboard ITBA",
        "══════════════════════════════════════",
        "",
        f"Generado : {generated_at.strftime('%d/%m/%Y %H:%M')} UTC",
        "",
        "── Dashboard ──────────────────────────",
    ]
    if dashboard_url:
        lines.extend(["Abrir panel:", dashboard_url, ""])
    else:
        lines.append("(URL del dashboard no configurada)")
        lines.append("")

    lines.extend(["── Monto total por moneda ─────────────"])
    if currencies:
        width = max(len(currency) for currency in currencies)
        for currency, amount in sorted(currencies.items()):
            lines.append(f"  {currency:<{width}}  {_format_amount(amount)}")
    else:
        lines.append("  Sin montos disponibles")

    lines.extend(["", "── Usuarios con más eventos ───────────"])
    for user_id, count in users.most_common(5):
        lines.append(f"  {user_id:<32} {count:>4}")
    if not users:
        lines.append("  —")

    lines.extend(["", "── Países / canales principales ────────"])
    for country, count in countries.most_common(5):
        lines.append(f"  País {country:<24} {count:>4}")
    for channel, count in channels.most_common(5):
        lines.append(f"  Canal {channel:<23} {count:>4}")

    lines.extend(
        [
            "",
            "── Transacciones de mayor riesgo ───────",
            f"  {'TX':<18} {'USUARIO':<18} {'MONTO':>12} {'MON':<4} {'PAÍS':<6} {'CANAL':<8} {'SCORE':>7}",
            f"  {'-' * 18} {'-' * 18} {'-' * 12} {'-' * 4} {'-' * 6} {'-' * 8} {'-' * 7}",
        ]
    )
    for row in highest_risk:
        lines.append(
            f"  {str(row.get('transaction_id') or '')[:18]:<18} "
            f"{str(row.get('user_id') or '')[:18]:<18} "
            f"{_format_amount(_parse_decimal(row.get('amount'))):>12} "
            f"{str(row.get('currency') or '')[:4]:<4} "
            f"{str(row.get('country') or '')[:6]:<6} "
            f"{str(row.get('channel') or '')[:8]:<8} "
            f"{_format_score(row.get('fraud_score')):>7}"
        )
    if not highest_risk:
        lines.append("  —")

    lines.extend(["", "──────────────────────────────────────"])
    lines.append("Correo automático del laboratorio Fraud Detector.")

    return "\n".join(lines)


def _build_summary(events, *, dashboard_url):
    rows, currencies, users, countries, channels, highest_risk = _aggregate_events(events)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)

    text_body = _build_summary_text(
        rows,
        currencies,
        users,
        countries,
        channels,
        highest_risk,
        generated_at=generated_at,
        dashboard_url=dashboard_url,
    )
    return text_body, len(rows)


EMAIL_SUBJECT = "Resumen de fraude — Dashboard ITBA"


def _publish_summary(topic_arn, subject, text_body):
    # SNS email subscriptions only deliver plain text (no HTML rendering).
    sns.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=text_body,
    )


def handler(event, context):
    queue_url = os.environ["FRAUD_ALERT_QUEUE_URL"]
    topic_arn = os.environ["SUMMARY_TOPIC_ARN"]
    max_messages = int(os.environ.get("MAX_MESSAGES_PER_RUN", "500"))
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()

    messages = _receive_messages(queue_url, max_messages)
    if not messages:
        _safe_log("fraud_summary_empty", message_count=0, event_count=0)
        return {"published": False, "messages": 0}

    events = []
    parse_errors = 0
    for message in messages:
        try:
            events.append(_parse_payload(message["Body"]))
        except Exception:
            parse_errors += 1

    text_body, event_count = _build_summary(
        events,
        dashboard_url=dashboard_url,
    )
    trace_count, trace_samples = _trace_summary(events)
    if event_count == 0:
        _delete_messages(queue_url, messages)
        _safe_log(
            "fraud_summary_no_valid_events",
            message_count=len(messages),
            event_count=0,
            parse_error_count=parse_errors,
            trace_id_count=trace_count,
            trace_id_samples=trace_samples,
        )
        return {"published": False, "messages": len(messages), "events": 0}

    _publish_summary(
        topic_arn,
        EMAIL_SUBJECT,
        text_body,
    )
    _delete_messages(queue_url, messages)

    _safe_log(
        "fraud_summary_published",
        message_count=len(messages),
        event_count=event_count,
        parse_error_count=parse_errors,
        trace_id_count=trace_count,
        trace_id_samples=trace_samples,
    )
    return {"published": True, "messages": len(messages), "events": event_count}
