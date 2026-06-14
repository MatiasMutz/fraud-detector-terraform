# `modules/data_store`

Provisions all persistent storage for the fraud-scoring system: a DynamoDB table for user behaviour features (read during scoring) and a PostgreSQL RDS instance for fraud results (written after scoring). Both stores live in the same module because they share the data-store concern, but they serve opposite ends of the pipeline.

## Resources

### DynamoDB — user behaviour (scoring input)

- `aws_dynamodb_table.user_behavior` — `<project>-user-behavior`.
  - `billing_mode = PAY_PER_REQUEST` (no capacity planning needed for the lab).
  - `hash_key = user_id` (string), no sort key.
  - Server-side encryption enabled (AWS-owned key).
  - Point-In-Time Recovery enabled by default.
  - Deletion protection toggleable (off by default for lab cleanup).

### RDS PostgreSQL — fraud results (scoring output)

- `aws_db_subnet_group.results` — `<project>-results-db-subnet-group`. Spans the data-tier subnets passed in `private_subnet_ids` (≥2 AZs required by RDS, even for single-AZ deployments).
- `aws_security_group.rds` — `<project>-rds-sg`. No ingress rules are created inside this module; they are added in the root composition to avoid circular module dependencies.
- `aws_db_instance.results` — `<project>-results-db`.
  - Engine: PostgreSQL 17.10, `db.t3.micro`, 20 GiB gp2.
  - `storage_encrypted = true` (AWS-owned key; KMS-CMK not available in Academy).
  - Single-AZ, no automated backups, `skip_final_snapshot = true` — lab cost optimisations.
  - `lifecycle { ignore_changes = [password] }` so Terraform does not attempt to re-set the password on subsequent plans.

## Inputs

| Name                            | Type           | Default           | Description                                                                       |
| ------------------------------- | -------------- | ----------------- | --------------------------------------------------------------------------------- |
| `project`                       | `string`       | n/a               | Prefix for all resource names.                                                    |
| `tags`                          | `map(string)`  | `{}`              | Common tags merged with `Component = "data-store"`.                               |
| `hash_key_name`                 | `string`       | `"user_id"`       | DynamoDB partition key attribute name.                                            |
| `enable_point_in_time_recovery` | `bool`         | `true`            | Toggle PITR for the DynamoDB table.                                               |
| `enable_deletion_protection`    | `bool`         | `false`           | Toggle DynamoDB deletion protection.                                              |
| `vpc_id`                        | `string`       | n/a               | VPC where the RDS instance is deployed.                                           |
| `private_subnet_ids`            | `list(string)` | n/a               | Data-tier subnet IDs for the DB subnet group and RDS Proxy (≥2 required).                        |
| `instance_class`                | `string`       | `"db.t3.micro"`   | RDS instance class.                                                               |
| `db_name`                       | `string`       | `"fraud_results"` | Initial database name in PostgreSQL.                                              |
| `db_username`                   | `string`       | `"fraud_admin"`   | Master username for the RDS instance.                                             |
| `db_password`                   | `string`       | n/a               | Master password. `sensitive = true`. Generate with `random_password` in the root. |
| `principal_arn`                 | `string`       | n/a               | LabRole ARN used by RDS Proxy to read the Secrets Manager secret.                 |

## Outputs

### DynamoDB

| Name            | Description                                 |
| --------------- | ------------------------------------------- |
| `table_name`    | Name of the DynamoDB table.                 |
| `table_arn`     | ARN of the table.                           |
| `table_id`      | Internal table id (same as name).           |
| `hash_key_name` | Partition key attribute name (echoed back). |

### RDS

| Name                    | Description                                                                                                           |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `db_address`            | Direct RDS hostname (no port). Lambdas should use `proxy_endpoint` as `DB_HOST`.                                      |
| `db_port`               | RDS port (5432).                                                                                                      |
| `db_name`               | Initial database name.                                                                                                |
| `db_username`           | Master username.                                                                                                      |
| `db_endpoint`           | Full endpoint in `host:port` format.                                                                                  |
| `rds_security_group_id`     | RDS security group ID. Exposed so the root composition can add rules without circular module dependencies.          |
| `db_instance_id`            | RDS instance identifier.                                                                                             |
| `proxy_endpoint`            | Hostname of the RDS Proxy. Use as `DB_HOST` in Lambdas instead of the direct RDS endpoint.                          |
| `proxy_security_group_id`   | RDS Proxy security group ID. Exposed so the root composition can add Lambda ingress rules to the proxy.              |

## Example

```hcl
module "data_store" {
  source = "./modules/data_store"

  project            = local.project
  principal_arn      = data.aws_iam_role.lab.arn
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.data_subnet_ids
  db_password        = random_password.db.result
  tags               = local.common_tags
}
```

Lambdas connect to RDS **via RDS Proxy** — the `proxy_endpoint` output is passed as `db_host`. Cross-module security group rules (Lambda → Proxy) must be created in the root to avoid circular dependencies:

```hcl
resource "aws_vpc_security_group_ingress_rule" "proxy_from_writer" {
  security_group_id            = module.data_store.proxy_security_group_id
  referenced_security_group_id = module.results_writer.lambda_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
```

## Notes for AWS Academy

- **DynamoDB**: AWS Academy does not allow customer-managed KMS keys. The table uses the AWS-owned default key (Checkov `CKV_AWS_119` skipped).
- **RDS**: `auto_minor_version_upgrade`, `copy_tags_to_snapshot`, and `iam_database_authentication_enabled` are enabled. Applications still authenticate via RDS Proxy and Secrets Manager (`iam_auth = DISABLED` on the proxy). Checkov `CKV_AWS_157`, `CKV_AWS_133`, `CKV_AWS_118`, `CKV_AWS_293`, `CKV_AWS_129`, `CKV_AWS_354` remain skipped — lab cost or Academy restriction trade-offs documented inline.
- **Audit S3**: lifecycle includes `abort_incomplete_multipart_upload` after 7 days.
- The RDS password is generated with `random_password` in the root composition and passed as a sensitive variable. Retrieve it with `terraform output -raw db_password`.
- The TP requires only one DynamoDB table — there are no GSIs or LSIs by design.
