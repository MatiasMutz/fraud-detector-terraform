# Processor

SQS-driven fraud scoring worker deployed to **ECS Fargate** (`cmd/worker`). It is the only production entrypoint for this service.

## Production flow

1. Long-poll the ingestion SQS queue (`QUEUE_URL`).
2. Load the user profile from DynamoDB (`DYNAMODB_TABLE_NAME`).
3. Score with the Go ML runtime (`FRAUD_RUNTIME_SPEC_PATH`) or rule-based fallback.
4. Publish every result to the results SQS queue (`RESULTS_QUEUE_URL`).
5. Publish fraudulent results to the fraud-alert SQS queue (`FRAUD_ALERT_QUEUE_URL`).
6. Optionally write an audit JSON object to S3 (`S3_AUDIT_BUCKET`).

Producers receive only the SQS `SendMessage` acknowledgment. Scored outcomes are persisted by `results-writer` Lambda into RDS and exposed through the dashboard HTTP API.

## Trace logging

Every accepted message carries a `trace_id`. Producers should set it to a random UUID; legacy messages without it receive a deterministic hashed fallback inside the processor. CloudWatch logs are JSON and intentionally omit transaction IDs, user IDs, amounts, countries, channels, scores, decisions, payloads, and raw receipt handles.

The processor emits one wide `tx_processed` INFO log per processed message with step durations, SQS age, receive count, S3 status, publish status, and ack status. Downstream components use the same `trace_id` for correlation.

## Build

From the repository root:

```bash
make prepare-model   # when runtime_spec.json is missing
docker build -f app/processor/Dockerfile -t fraud-worker:local app
```

The image entrypoint is `/fraud-worker` (see `app/processor/Dockerfile`).

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `QUEUE_URL` | yes | Ingestion SQS queue URL |
| `QUEUE_NAME` | yes | Ingestion queue name (metrics) |
| `RESULTS_QUEUE_URL` | yes | Results SQS queue URL |
| `FRAUD_ALERT_QUEUE_URL` | yes | Fraud-alert SQS queue URL |
| `DYNAMODB_TABLE_NAME` | yes | User-behavior table name |
| `AWS_REGION` | yes | AWS region |
| `S3_AUDIT_BUCKET` | no | Audit bucket for raw JSON events |
| `FRAUD_RUNTIME_SPEC_PATH` | no | ML spec path (default search paths under `model/`) |
| `PROCESSOR_CONCURRENCY` | no | Workers per task (default `32`) |
| `PROCESSOR_POLLERS` | no | SQS long-pollers per task (default `4`) |

## Tests

```bash
cd app/processor
go test ./...
```

## ML runtime spec

`model/runtime_spec.json` is bundled into the container image. Regenerate it from the training pipeline with `make prepare-model` at the repository root.
