# `modules/results_writer`

Composition module for the buffered path between the processor and the RDS results store. It owns the results SQS queue plus DLQ, a VPC-attached Lambda, and the event source mapping that drains SQS into PostgreSQL.

The Lambda now uses the Lambda OS-only/custom runtime path: the deployment package passed through `package_file` must contain an executable named `bootstrap` at the zip root. Root workflows build that zip from `app/results_writer` before Terraform evaluates the Lambda `source_code_hash`.

The writer batches the full SQS invocation into a single database transaction and performs bulk `INSERT ... VALUES ... ON CONFLICT (transaction_id) DO NOTHING` statements. The RDS schema is migrated by a direct-only action on the API Lambda and must be migrated before writer traffic is processed. Raw JSON payloads and SNS-style envelopes with a `Message` JSON body are both accepted. `trace_id` and `ingested_at` are persisted when present, and batch logs include counts, latency summaries, and trace samples without transaction business data.

## Resources

- `aws_sqs_queue.results_dlq` - `<project>-results-events-dlq`. 14-day retention, SSE-SQS.
- `aws_sqs_queue_redrive_allow_policy.results_dlq` - restricts the DLQ to the main results queue only.
- `aws_sqs_queue.results` - `<project>-results-events`. Visibility timeout 360 s, SSE-SQS, redrive to DLQ after 3 receives.
- `aws_sqs_queue_policy.results` - allows `principal_arn` to send and consume, and denies insecure transport.
- `aws_cloudwatch_log_group.writer` - `/aws/lambda/<project>-results-writer`. 30-day retention by default.
- `aws_security_group.writer_lambda` - `<project>-writer-lambda-sg`. No ingress; egress to the VPC endpoint SG on tcp/443. The egress rule to the RDS Proxy SG is created in the root composition to avoid circular module dependencies.
- `aws_lambda_function.writer` - `<project>-results-writer`. `provided.al2023`, `bootstrap` handler, x86_64, 1024 MiB, 60 s timeout, deployed in private subnets. Receives `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` and connects to PostgreSQL with the Go `lib/pq` driver.
- `aws_lambda_event_source_mapping.sqs_results` - triggers the Lambda from the results SQS queue. Batch size and batching window are module variables and default to large values.

## Inputs

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `project` | `string` | n/a | Prefix for resource names. |
| `tags` | `map(string)` | `{}` | Common tags merged with `Component = "results-writer"`. |
| `principal_arn` | `string` | n/a | IAM role ARN used for Lambda execution and SQS permissions. |
| `vpc_id` | `string` | n/a | VPC where the Lambda is deployed. |
| `private_subnet_ids` | `list(string)` | n/a | App-tier subnets for the Lambda VPC config. |
| `endpoint_security_group_id` | `string` | n/a | VPC endpoint SG; the Lambda opens egress tcp/443 here. |
| `db_host` | `string` | n/a | RDS Proxy endpoint hostname (`DB_HOST`). |
| `db_port` | `number` | `5432` | RDS port (`DB_PORT`). |
| `db_name` | `string` | `"fraud_results"` | Database name (`DB_NAME`). |
| `db_username` | `string` | `"fraud_admin"` | Database user (`DB_USER`). |
| `db_password` | `string` | n/a | Database password (`DB_PASSWORD`), marked sensitive. |
| `package_file` | `string` | n/a | Local zip package path built from `app/results_writer`. |
| `sqs_batch_size` | `number` | `5000` | Maximum records per SQS batch for the writer Lambda. Standard SQS queues allow up to 10,000. |
| `sqs_batching_window_seconds` | `number` | `5` | Maximum batching window for the event source mapping. Must be at least 1 when `sqs_batch_size > 10`. |
| `log_retention_days` | `number` | `30` | CloudWatch Logs retention days. |

## Outputs

| Name | Description |
| --- | --- |
| `queue_arn` | ARN of the results SQS queue. |
| `queue_url` | URL of the results SQS queue. |
| `queue_name` | Name of the results SQS queue. |
| `dlq_arn` | ARN of the DLQ. |
| `lambda_function_name` | Name of the results-writer Lambda. |
| `lambda_function_arn` | ARN of the results-writer Lambda. |
| `lambda_security_group_id` | Lambda SG ID exposed for the root RDS Proxy egress rule. |
| `log_group_name` | CloudWatch log group name. |

## Build artifact

The deployment package is expected from the root composition at `app/results_writer/build/results-writer.zip`. A compatible build should:

1. Compile the Go binary for Linux x86_64.
2. Name the binary `bootstrap`.
3. Zip that binary at the root of the archive.

The generated zip is ignored by git. Use `make build-results-writer` before any direct `terraform plan`, `terraform apply`, or `terraform validate` call; the repo Make targets and GitHub Actions workflows run that build step before Terraform.

## Example

```hcl
module "results_writer" {
  source = "./modules/results_writer"

  project                    = local.project
  principal_arn              = data.aws_iam_role.lab.arn
  vpc_id                     = module.network.vpc_id
  private_subnet_ids         = module.network.app_subnet_ids
  endpoint_security_group_id = module.network.endpoint_security_group_id
  db_host                    = module.data_store.proxy_endpoint
  db_port                    = module.data_store.db_port
  db_name                    = module.data_store.db_name
  db_username                = module.data_store.db_username
  db_password                = random_password.db.result
  package_file               = "${path.root}/app/results_writer/build/results-writer.zip"
  sqs_batch_size              = 5000
  sqs_batching_window_seconds = 5
  tags                        = local.common_tags
}
```

## Notes for AWS Academy

- KMS-CMK for SNS, SQS, and CloudWatch Logs is not available in Academy; resources use AWS-owned or SQS-managed encryption where supported.
- `LabRole` is reused for Lambda execution and queue access because the lab restricts IAM role creation.
- The Lambda code lives under `app/results_writer`; this module only owns infrastructure and consumes the packaged artifact path.
- The writer keeps malformed messages retryable by failing the full invocation, and it uses `ON CONFLICT (transaction_id) DO NOTHING` to make replays idempotent.
