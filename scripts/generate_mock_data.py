#!/usr/bin/env python3
"""
Seed RDS with realistic mock fraud-scoring data.

Invokes the results-writer Lambda directly with synthetic SQS records,
bypassing the on-prem SQS CIDR restriction entirely.

Usage:
    python3 scripts/generate_mock_data.py [options]

Options:
    --function   Lambda function name  (default: itba-tp-fraud-results-writer)
    --region     AWS region            (default: us-east-1)
    --count      Target record count   (default: 1000)
    --days       Spread over N days    (default: 30)
    --batch      Records per Lambda invoke (default: 10)
    --dry-run    Print stats without invoking Lambda

Run via Makefile:
    make seed
    make seed COUNT=500
"""

import argparse
import json
import math
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

import boto3

# ── Country / currency / channel tables ───────────────────────────────────────

CURRENCY = {
    "AR": "ARS", "BR": "BRL", "MX": "MXN", "CO": "COP", "CL": "CLP",
    "UY": "UYU", "PE": "PEN", "PY": "PYG", "EC": "USD", "BO": "BOB",
    "US": "USD", "ES": "EUR", "GB": "GBP", "DE": "EUR", "CN": "CNY", "VE": "USD",
}

LATAM     = ["AR", "BR", "MX", "CO", "CL", "UY", "PE", "PY", "EC", "BO"]
WORLDWIDE = LATAM + ["US", "ES", "GB", "DE", "CN", "VE"]
CHANNELS  = ["web", "mobile", "atm", "branch", "pos", "TALO PAY 😉"]
ACCOUNTS  = [f"acc-{i:06d}" for i in range(1, 301)]

# Hour-of-day probability weights (index = hour UTC)
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 1, 2, 3, 5, 6, 6, 6, 5, 6, 6, 6, 5, 5, 4, 3, 3, 2, 2, 1]


# ── User profile builder ───────────────────────────────────────────────────────

def _user(uid, home, countries, avg_amt, channels, tx_count, risky=False):
    rng = random.Random(uid)  # deterministic account list per user
    return {
        "id":              uid,
        "home":            home,
        "countries":       countries,
        "avg_amount":      avg_amt,
        "channels":        channels,
        "tx_count":        tx_count,
        "risky":           risky,
        "known_accounts":  rng.sample(ACCOUNTS, min(rng.randint(3, 10), len(ACCOUNTS))),
    }


def build_profiles():
    """Return a fixed set of user profiles that sum to roughly 1 000 transactions."""
    p = []

    # ── 5 whale users (55-70 tx each, ~310 total) ────────────────────────────
    p += [
        _user("usr-ar-whale-01", "AR", ["AR", "UY", "CL", "BR"],   12_000, ["web", "branch"],     68),
        _user("usr-br-whale-01", "BR", ["BR", "US", "AR"],          18_000, ["web", "mobile"],     62),
        _user("usr-mx-whale-01", "MX", ["MX", "US", "CO"],          15_000, ["web", "branch"],     58),
        _user("usr-co-whale-01", "CO", ["CO", "US", "MX", "PE"],     9_000, ["mobile", "web"],     55),
        _user("usr-cl-whale-01", "CL", ["CL", "AR", "PE"],          11_000, ["web", "pos"],        50),
    ]

    # ── 10 frequent users (25-38 tx each, ~310 total) ────────────────────────
    freq = [
        ("AR", ["AR", "BR"],              2_500, ["web", "mobile", "atm"], 35),
        ("BR", ["BR", "US"],              4_000, ["web", "mobile"],        32),
        ("MX", ["MX", "US"],              3_000, ["mobile", "pos"],        30),
        ("CO", ["CO", "MX"],              1_800, ["mobile", "atm"],        28),
        ("CL", ["CL", "AR", "PE"],        3_500, ["web", "pos"],           27),
        ("UY", ["UY", "AR", "BR"],        2_200, ["web", "mobile"],        35),
        ("PE", ["PE", "CO", "EC"],        1_500, ["mobile", "atm"],        26),
        ("AR", ["AR"],                      900, ["atm", "branch"],        30),
        ("BR", ["BR", "AR"],              5_500, ["web", "branch"],        31),
        ("MX", ["MX", "US", "CO"],        4_200, ["web", "mobile"],        28),
    ]
    for i, (home, countries, avg, chans, tx) in enumerate(freq, 1):
        p.append(_user(f"usr-{home.lower()}-freq-{i:02d}", home, countries, avg, chans, tx))

    # ── 15 regular users (8-18 tx each, ~185 total) ──────────────────────────
    profiles_reg = [
        ("AR", ["AR"],                    800,  ["atm", "pos"],     12),
        ("BR", ["BR"],                  2_000,  ["mobile"],         14),
        ("MX", ["MX", "US"],            1_200,  ["web"],            10),
        ("CO", ["CO"],                    600,  ["mobile", "atm"],  16),
        ("CL", ["CL", "AR"],            1_800,  ["web", "pos"],      8),
        ("UY", ["UY"],                    900,  ["mobile", "branch"],15),
        ("PE", ["PE"],                    700,  ["atm"],            18),
        ("AR", ["AR", "CL"],            3_000,  ["web"],            11),
        ("BR", ["BR", "US"],            5_000,  ["web", "mobile"],  13),
        ("MX", ["MX"],                    400,  ["pos", "atm"],     14),
        ("CO", ["CO", "EC"],            1_100,  ["mobile"],          9),
        ("CL", ["CL"],                  2_200,  ["web", "branch"],  10),
        ("AR", ["AR", "UY"],              750,  ["mobile"],         17),
        ("EC", ["EC", "CO", "PE"],        500,  ["mobile", "atm"],  12),
        ("BO", ["BO", "PE", "AR"],        600,  ["branch"],          8),
    ]
    for i, (home, countries, avg, chans, tx) in enumerate(profiles_reg, 1):
        p.append(_user(f"usr-{home.lower()}-reg-{i:02d}", home, countries, avg, chans, tx))

    # ── 15 occasional users (2-7 tx each, ~60 total) ─────────────────────────
    occ_data = [
        ("AR", ["AR"],           300, ["pos"],          4),
        ("BR", ["BR"],           500, ["atm"],          3),
        ("MX", ["MX"],           800, ["mobile"],       5),
        ("CO", ["CO"],           200, ["atm"],          2),
        ("CL", ["CL"],           900, ["web"],          6),
        ("UY", ["UY"],           400, ["mobile"],       3),
        ("PE", ["PE"],           350, ["branch"],       7),
        ("PY", ["PY"],           250, ["atm"],          4),
        ("EC", ["EC"],           600, ["mobile"],       3),
        ("BO", ["BO"],           180, ["branch"],       2),
        ("AR", ["AR"],           700, ["web"],          5),
        ("BR", ["BR"],         1_200, ["web"],          4),
        ("MX", ["MX"],           550, ["pos"],          3),
        ("CO", ["CO"],           430, ["atm"],          2),
        ("CL", ["CL"],           800, ["mobile"],       5),
    ]
    for i, (home, countries, avg, chans, tx) in enumerate(occ_data, 1):
        p.append(_user(f"usr-{home.lower()}-occ-{i:02d}", home, countries, avg, chans, tx))

    # ── 5 risky users (15-28 tx each, ~110 total) ────────────────────────────
    risky = [
        ("MX", WORLDWIDE[:8],             8_000, ["web", "mobile", "atm"], 25),
        ("VE", WORLDWIDE,                15_000, ["web", "atm"],            18),
        ("CO", ["CO","US","MX","VE","EC"],12_000, ["web", "mobile"],        22),
        ("AR", ["AR","BR","UY","ES","US"],7_000, ["web", "pos"],            15),
        ("BR", ["BR","US","DE","CN"],     20_000, ["web", "branch"],        28),
    ]
    for i, (home, countries, avg, chans, tx) in enumerate(risky, 1):
        p.append(_user(f"usr-{home.lower()}-risky-{i:02d}", home, countries, avg, chans, tx, risky=True))

    return p


# ── Fraud scoring (mirrors Fargate rule-based logic) ──────────────────────────

def score(profile, country, amount, dest_account):
    s = 0.0

    # Amount vs typical
    ratio = amount / max(profile["avg_amount"], 1)
    if ratio > 5:    s += 0.45
    elif ratio > 3:  s += 0.30
    elif ratio > 2:  s += 0.15

    # Country novelty
    if country not in profile["countries"]:
        s += 0.28
        if country in ("VE", "CN", "DE", "GB") and profile["home"] not in ("VE", "CN", "DE", "GB"):
            s += 0.12

    # Unknown destination
    if dest_account not in profile["known_accounts"]:
        s += 0.08

    # High absolute amount
    if amount > 50_000:   s += 0.25
    elif amount > 20_000: s += 0.10

    # Risky profile base
    if profile["risky"]:
        s += 0.28

    # Gaussian noise
    s += random.gauss(0, 0.04)
    s = round(max(0.0, min(1.0, s)), 4)
    return s, s >= 0.70


# ── Timestamp generator ───────────────────────────────────────────────────────

def random_ts(days_back):
    day_offset = random.randint(0, days_back - 1)
    base = datetime.now(timezone.utc) - timedelta(days=day_offset)
    hour = random.choices(range(24), weights=HOUR_WEIGHTS)[0]
    return base.replace(hour=hour, minute=random.randint(0, 59), second=random.randint(0, 59), microsecond=0)


# ── Transaction factory ───────────────────────────────────────────────────────

def make_transactions(profiles, days_back):
    """
    Generate transactions with explicit anomaly injection so the fraud rate
    is realistic (~8-12% overall, ~40-50% for risky users).

    Anomaly = foreign country (not in user's list) + amount spike (4-8x avg).
    This guarantees the rule-based scorer crosses the 0.70 threshold.
    """
    txs = []
    for profile in profiles:
        # Probability a given transaction is an anomaly
        anomaly_prob = 0.45 if profile["risky"] else 0.05

        for _ in range(profile["tx_count"]):
            is_anomaly = random.random() < anomaly_prob

            if is_anomaly:
                foreign = [c for c in WORLDWIDE if c not in profile["countries"]]
                country  = random.choice(foreign) if foreign else random.choice(WORLDWIDE)
                amount   = round(profile["avg_amount"] * random.uniform(4.5, 9.0), 2)
            else:
                country = random.choice(profile["countries"])
                amount  = round(max(1.0, random.lognormvariate(math.log(max(profile["avg_amount"], 1)), 0.45)), 2)

            dest  = random.choice(ACCOUNTS)
            fraud_score, is_fraud = score(profile, country, amount, dest)
            ts    = random_ts(days_back)

            txs.append({
                "trace_id":       str(uuid.uuid4()),
                "transaction_id": str(uuid.uuid4()),
                "user_id":        profile["id"],
                "amount":         amount,
                "currency":       CURRENCY.get(country, "USD"),
                "country":        country,
                "channel":        random.choice(profile["channels"]),
                "fraud_score":    fraud_score,
                "is_fraud":       is_fraud,
                "processed_at":   ts.isoformat(),
                "_ts":            ts,
            })

    txs.sort(key=lambda t: t["_ts"])
    return txs


# ── Lambda invocation ─────────────────────────────────────────────────────────

def _sqs_record(tx):
    payload = {k: v for k, v in tx.items() if k != "_ts"}
    return {
        "messageId":      str(uuid.uuid4()),
        "receiptHandle":  str(uuid.uuid4()),
        "body":           json.dumps(payload),
        "attributes": {
            "ApproximateFirstReceiveTimestamp": str(int(tx["_ts"].timestamp() * 1000)),
            "SentTimestamp":                    str(int(tx["_ts"].timestamp() * 1000)),
        },
        "messageAttributes": {},
        "md5OfBody":      "",
        "eventSource":    "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:us-east-1:000000000000:mock",
        "awsRegion":      "us-east-1",
    }


def invoke_batch(client, function_name, batch):
    event = {"Records": [_sqs_record(tx) for tx in batch]}
    resp  = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"Lambda error: {body}")
    return body.get("processed", 0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Seed RDS with mock fraud data via Lambda.")
    ap.add_argument("--function", default="itba-tp-fraud-results-writer", metavar="NAME")
    ap.add_argument("--region",   default="us-east-1")
    ap.add_argument("--count",    type=int, default=1000, help="Target record count (approximate)")
    ap.add_argument("--days",     type=int, default=30,   help="Spread records over N days back")
    ap.add_argument("--batch",    type=int, default=10,   help="Records per Lambda invocation")
    ap.add_argument("--dry-run",  action="store_true",    help="Print stats only, do not invoke Lambda")
    args = ap.parse_args()

    profiles = build_profiles()
    base_total = sum(p["tx_count"] for p in profiles)

    # Scale tx_count proportionally to reach args.count
    if args.count != base_total:
        factor = args.count / base_total
        for p in profiles:
            p["tx_count"] = max(1, round(p["tx_count"] * factor))

    transactions = make_transactions(profiles, args.days)
    total        = len(transactions)
    n_fraud      = sum(1 for t in transactions if t["is_fraud"])
    users        = len({t["user_id"] for t in transactions})

    print(f"  Records   : {total}")
    print(f"  Fraudulent: {n_fraud} ({n_fraud/total*100:.1f}%)")
    print(f"  Users     : {users}")
    print(f"  Date range: {transactions[0]['_ts'].date()} → {transactions[-1]['_ts'].date()}")
    print(f"  Countries : {sorted({t['country'] for t in transactions})}")
    print(f"  Channels  : {sorted({t['channel'] for t in transactions})}")

    if args.dry_run:
        print("\n[dry-run] No records sent.")
        return

    client   = boto3.client("lambda", region_name=args.region)
    sent     = 0
    errors   = 0
    batches  = [transactions[i:i + args.batch] for i in range(0, total, args.batch)]
    n_batch  = len(batches)

    print(f"\nSending {total} records in {n_batch} batches to '{args.function}'...\n")

    for idx, batch in enumerate(batches, 1):
        try:
            processed = invoke_batch(client, args.function, batch)
            sent += processed
        except Exception as exc:
            errors += 1
            print(f"  [ERROR] batch {idx}/{n_batch}: {exc}", file=sys.stderr)

        if idx % 10 == 0 or idx == n_batch:
            pct = idx / n_batch * 100
            bar = "█" * (idx * 30 // n_batch) + "░" * (30 - idx * 30 // n_batch)
            print(f"\r  [{bar}] {pct:5.1f}%  {sent}/{total} sent  {errors} errors", end="", flush=True)

    print(f"\n\nDone. {sent} records written to RDS. {errors} batch errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
