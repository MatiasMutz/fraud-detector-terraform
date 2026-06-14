#!/usr/bin/env python3
"""Continuously enqueue synthetic transactions to the ingestion SQS queue."""

from __future__ import annotations

import json
import logging
import math
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

COUNTRIES = ["AR", "BR", "CL", "UY", "MX", "CO", "PE", "US"]
CHANNELS = ["web", "mobile", "atm", "pos", "api"]
NORMAL_SCENARIOS = ["trusted_repeat", "returning_daily", "new_user_sparse"]
FRAUD_SCENARIOS = [
    "account_drain",
    "country_shift",
    "device_shift",
    "merchant_fanout",
    "micro_amount_card_testing",
]
CURRENCY_BY_COUNTRY = {
    "AR": "ARS",
    "BR": "BRL",
    "CL": "CLP",
    "UY": "UYU",
    "MX": "MXN",
    "CO": "COP",
    "PE": "PEN",
    "US": "USD",
}

LOG_PATH = "/var/log/onprem-tx-producer.log"


def env_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        if default is None:
            raise RuntimeError(f"{name} is required")
        return default
    return int(raw)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout),
        ],
    )


def pick(values: list[str]) -> str:
    return random.choice(values)


def pick_float(values: list[float]) -> float:
    return random.choice(values)


def rng_float(low: float, high: float) -> float:
    return low + random.random() * (high - low)


def round_value(value: float, digits: int = 2) -> float:
    return round(value, digits)


def choose_user() -> str:
    if random.random() < 0.85:
        return f"u-{random.randint(1, 2000):04d}"
    return f"new-{time.time_ns()}-{random.randint(0, 99999)}"


def user_home_country(user_id: str) -> str:
    return COUNTRIES[sum(ord(char) for char in user_id) % len(COUNTRIES)]


def alternate_country(home: str) -> str:
    options = [country for country in COUNTRIES if country != home]
    return random.choice(options)


def feature_blob(is_fraud: bool, amount: float) -> dict[str, float]:
    base: dict[str, float] = {
        "amount": amount,
        "amount_log1p": round_value(math.log1p(amount), 6),
    }

    if is_fraud:
        prior_mean = rng_float(20, 900)
        dest_unique_5 = random.randint(3, 5)
        base.update(
            {
                "card1": float(random.randint(1650, 2250)),
                "card2": float(random.randint(120, 220)),
                "card3": float(random.randint(140, 190)),
                "card5": float(random.randint(210, 240)),
                "addr1": float(random.randint(260, 380)),
                "addr2": float(random.randint(55, 75)),
                "dist1": rng_float(4, 18),
                "dist2": rng_float(3, 16),
                "m1": 0,
                "m2": 0,
                "m3": 0,
                "m5": float(random.randint(0, 1)),
                "m6": 0,
                "m7": 0,
                "m8": 0,
                "m9": 0,
                "c1": rng_float(3, 7),
                "c2": rng_float(3.5, 8),
                "c4": rng_float(2, 5.5),
                "c7": rng_float(2, 6),
                "c10": rng_float(2, 5),
                "c14": rng_float(2.5, 6.5),
                "d1": rng_float(0.001, 0.08),
                "d2": rng_float(12, 70),
                "d3": rng_float(2, 8),
                "d4": rng_float(2, 9),
                "d5": rng_float(3, 12),
                "id_01": rng_float(-9, -4.5),
                "id_02": rng_float(20, 95),
                "id_05": rng_float(0.1, 1.6),
                "id_11": rng_float(0.1, 1.2),
                "id_17": rng_float(6, 12),
                "id_23": rng_float(7, 14),
                "id_30": rng_float(3, 7),
                "id_33": rng_float(0.01, 1),
                "id_38": rng_float(2.5, 5.5),
                "previous_transaction_amount": rng_float(5, max(50, prior_mean)),
                "prior_5_transaction_count": float(random.randint(2, 5)),
                "prior_10_transaction_count": float(random.randint(5, 10)),
                "prior_5_amount_sum": rng_float(prior_mean * 2, prior_mean * 5),
                "prior_5_amount_mean": prior_mean,
                "prior_5_amount_std": rng_float(5, max(8, prior_mean * 0.35)),
                "prior_10_amount_sum": rng_float(prior_mean * 5, prior_mean * 10),
                "prior_10_amount_mean": rng_float(prior_mean * 0.8, prior_mean * 1.2),
                "prior_10_amount_std": rng_float(8, max(12, prior_mean * 0.45)),
                "seconds_since_previous_transaction": pick_float(
                    [rng_float(5, 90), rng_float(90, 900), rng_float(900, 3600)]
                ),
                "prior_5_unique_name_dest_count": float(dest_unique_5),
                "prior_10_unique_name_dest_count": float(dest_unique_5 + random.randint(0, 6)),
            }
        )
        v_min, v_max = 3.0, 5.5
    else:
        prior_mean = max(25.0, amount * rng_float(0.75, 1.3))
        base.update(
            {
                "card1": float(random.randint(900, 1500)),
                "card2": float(random.randint(90, 170)),
                "card3": float(random.randint(150, 180)),
                "card5": float(random.randint(215, 230)),
                "addr1": float(random.randint(180, 260)),
                "addr2": float(random.randint(50, 65)),
                "dist1": rng_float(0, 2),
                "dist2": rng_float(0, 2),
                "m1": 1,
                "m2": 1,
                "m3": 1,
                "m5": 1,
                "m6": 1,
                "m7": 1,
                "m8": 1,
                "m9": 1,
                "c1": rng_float(0.4, 2),
                "c2": rng_float(0.4, 2),
                "c4": rng_float(0, 0.9),
                "c7": rng_float(0.2, 1.8),
                "c10": rng_float(0.2, 1.5),
                "c14": rng_float(0.1, 1.2),
                "d1": rng_float(2, 18),
                "d2": rng_float(0.1, 3),
                "d3": rng_float(0.1, 2),
                "d4": rng_float(0.1, 2),
                "d5": rng_float(0.1, 1.5),
                "id_01": rng_float(-3.5, -0.2),
                "id_02": rng_float(90, 180),
                "id_05": rng_float(1.5, 5),
                "id_11": rng_float(1.2, 4),
                "id_17": rng_float(0.1, 2),
                "id_23": rng_float(0.1, 1.5),
                "id_30": rng_float(0.3, 2),
                "id_33": rng_float(8, 24),
                "id_38": rng_float(0.2, 1.5),
                "previous_transaction_amount": rng_float(prior_mean * 0.6, prior_mean * 1.4),
                "prior_5_transaction_count": float(random.randint(1, 5)),
                "prior_10_transaction_count": float(random.randint(3, 10)),
                "prior_5_amount_sum": rng_float(prior_mean * 2, prior_mean * 5),
                "prior_5_amount_mean": prior_mean,
                "prior_5_amount_std": rng_float(2, max(4, prior_mean * 0.25)),
                "prior_10_amount_sum": rng_float(prior_mean * 5, prior_mean * 10),
                "prior_10_amount_mean": rng_float(prior_mean * 0.85, prior_mean * 1.15),
                "prior_10_amount_std": rng_float(4, max(6, prior_mean * 0.30)),
                "seconds_since_previous_transaction": pick_float(
                    [rng_float(3600, 21600), rng_float(21600, 86400), rng_float(86400, 604800)]
                ),
                "prior_5_unique_name_dest_count": float(random.randint(1, 2)),
                "prior_10_unique_name_dest_count": float(random.randint(1, 3)),
            }
        )
        v_min, v_max = 0.05, 0.9

    for name in ("v1", "v20", "v61", "v81", "v101", "v130", "v181", "v201", "v241", "v280", "v307", "v320"):
        base[name] = round_value(rng_float(v_min, v_max), 3)

    return {key: round_value(value, 3) for key, value in base.items()}


def make_transaction(index: int, fraud_pct: int) -> tuple[str, str, bool]:
    is_fraud = random.randint(0, 99) < fraud_pct
    scenario = pick(FRAUD_SCENARIOS if is_fraud else NORMAL_SCENARIOS)
    user_id = choose_user()
    home_country = user_home_country(user_id)
    country = home_country
    if scenario in {"country_shift", "account_drain"}:
        country = alternate_country(home_country)

    currency = CURRENCY_BY_COUNTRY[country]
    if is_fraud and random.random() < 0.35:
        currency = "USD"

    channel = pick(CHANNELS)
    if is_fraud:
        channel = pick(["web", "mobile", "api"])

    if is_fraud and scenario == "micro_amount_card_testing":
        amount = round_value(rng_float(1, 35))
        old_org = round_value(rng_float(100, 5000))
    elif is_fraud:
        old_org = round_value(rng_float(50000, 250000))
        amount = old_org if scenario == "account_drain" else round_value(rng_float(2500, min(180000, old_org)))
    else:
        old_org = round_value(rng_float(1000, 80000))
        amount = round_value(rng_float(25, max(50, min(old_org * 0.45, 3500))))

    old_dest = round_value(rng_float(0, 40000))
    if is_fraud and scenario in {"account_drain", "merchant_fanout"}:
        old_dest = 0.0

    dest_prefix = "acc" if is_fraud else "merchant"
    timestamp = datetime.now(timezone.utc) - timedelta(seconds=random.randint(0, 7 * 24 * 3600))

    message = {
        "trace_id": str(uuid.uuid4()),
        "transaction_id": f"{time.time_ns()}-{index}",
        "user_id": user_id,
        "amount": amount,
        "currency": currency,
        "timestamp": timestamp.isoformat(),
        "channel": channel,
        "destination_account": f"{dest_prefix}-{random.randint(1, 999999):06d}",
        "country": country,
        "oldbalance_org": old_org,
        "newbalance_orig": max(0.0, round_value(old_org - amount)),
        "oldbalance_dest": old_dest,
        "newbalance_dest": round_value(old_dest + amount),
        "features": feature_blob(is_fraud, amount),
    }
    return json.dumps(message, separators=(",", ":")), scenario, is_fraud


def send_batch(client, queue_url: str, bodies: list[str]) -> None:
    entries = [
        {"Id": f"m{index}", "MessageBody": body}
        for index, body in enumerate(bodies)
    ]
    client.send_message_batch(QueueUrl=queue_url, Entries=entries)


def main() -> None:
    setup_logging()
    queue_url = os.environ["QUEUE_URL"]
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    batch_size = env_int("BATCH_SIZE", 200)
    loop_interval = env_int("LOOP_INTERVAL_SEC", 6)
    fraud_pct = max(0, min(100, env_int("FRAUD_PCT", 20)))

    if batch_size <= 0:
        raise RuntimeError("BATCH_SIZE must be positive")
    if loop_interval <= 0:
        raise RuntimeError("LOOP_INTERVAL_SEC must be positive")

    client = boto3.client("sqs", region_name=region)
    total_sent = 0
    total_fraud = 0
    loops = 0
    started = time.monotonic()
    index = 1

    logging.info(
        "Starting continuous producer queue=%s batch_size=%d interval=%ds fraud_pct=%d",
        queue_url,
        batch_size,
        loop_interval,
        fraud_pct,
    )

    while True:
        loop_start = time.monotonic()
        bodies: list[str] = []
        loop_fraud = 0

        for _ in range(batch_size):
            body, _scenario, is_fraud = make_transaction(index, fraud_pct)
            bodies.append(body)
            index += 1
            if is_fraud:
                loop_fraud += 1

        try:
            for chunk_start in range(0, len(bodies), 10):
                send_batch(client, queue_url, bodies[chunk_start : chunk_start + 10])
        except (ClientError, BotoCoreError) as exc:
            logging.exception("Failed to send batch after %d message(s): %s", total_sent, exc)
            time.sleep(10)
            continue

        total_sent += batch_size
        total_fraud += loop_fraud
        loops += 1

        if loops % 10 == 0:
            elapsed = max(time.monotonic() - started, 0.001)
            logging.info(
                "Sent %d messages (fraud=%d, %.1f%%) at %.1f tx/s",
                total_sent,
                total_fraud,
                (total_fraud / total_sent) * 100 if total_sent else 0.0,
                total_sent / elapsed,
            )

        sleep_for = loop_interval - (time.monotonic() - loop_start)
        if sleep_for > 0:
            time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
