data "aws_caller_identity" "current" {}

locals {
  module_tags = merge(var.tags, {
    Service = "data-store"
  })

  table_name        = format("%s-user-behavior", var.project)
  db_identifier     = format("%s-results-db", var.project)
  audit_bucket_name = format("%s-audit-%s", var.project, data.aws_caller_identity.current.account_id)
}

resource "aws_dynamodb_table" "user_behavior" {
  # checkov:skip=CKV_AWS_119: AWS Academy no permite crear KMS CMK; SSE con clave AWS-owned cumple el requisito de cifrado.
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = var.hash_key_name

  deletion_protection_enabled = var.enable_deletion_protection

  attribute {
    name = var.hash_key_name
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.enable_point_in_time_recovery
  }

  tags = merge(local.module_tags, {
    Component = "dynamodb-table"
    Name      = local.table_name
  })
}

resource "aws_s3_bucket" "audit" {
  # checkov:skip=CKV_AWS_18: Access logging requeriría un bucket separado; fuera del alcance del lab.
  # checkov:skip=CKV_AWS_144: Replicación cross-region fuera del alcance del lab académico.
  # checkov:skip=CKV2_AWS_62: Notificaciones de eventos no requeridas en lab.
  bucket        = local.audit_bucket_name
  force_destroy = true

  tags = merge(local.module_tags, {
    Component = "s3-bucket"
    Name      = local.audit_bucket_name
    Role      = "audit"
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      # checkov:skip=CKV2_AWS_67: AWS Academy no permite KMS CMK; AES256 cumple el requisito de cifrado at-rest.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket = aws_s3_bucket.audit.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    id     = "expire-old-events"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_db_subnet_group" "results" {
  name       = format("%s-results-db-subnet-group", var.project)
  subnet_ids = var.private_subnet_ids

  tags = merge(local.module_tags, {
    Component = "db-subnet-group"
    Name      = format("%s-results-db-subnet-group", var.project)
  })
}

resource "aws_security_group" "rds" {
  name        = format("%s-rds-sg", var.project)
  description = "RDS PostgreSQL; ingress TCP/5432 desde Lambdas en VPC. Las reglas de ingress se definen en la composicion raiz para evitar dependencias circulares entre modulos."
  vpc_id      = var.vpc_id

  tags = merge(local.module_tags, {
    Component = "security-group"
    Name      = format("%s-rds-sg", var.project)
  })
}

resource "aws_db_instance" "results" {
  # checkov:skip=CKV_AWS_133: Enhanced Monitoring deshabilitado (lab; requiere rol IAM extra no disponible en Academy).
  # checkov:skip=CKV_AWS_118: Performance Insights deshabilitado (lab cost).
  # checkov:skip=CKV_AWS_293: Deletion protection deshabilitado para permitir terraform destroy en lab.
  # checkov:skip=CKV_AWS_129: Backup retention en 0 para reducir costo en lab académico.
  # checkov:skip=CKV_AWS_354: Dedicated log exports deshabilitados (lab).

  identifier = local.db_identifier

  engine         = "postgres"
  engine_version = "17.10"
  instance_class = var.instance_class

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  allocated_storage = 20
  storage_type      = "gp2"
  storage_encrypted = true

  db_subnet_group_name   = aws_db_subnet_group.results.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  multi_az                            = true
  backup_retention_period             = 0
  skip_final_snapshot                 = true
  deletion_protection                 = false
  apply_immediately                   = true
  auto_minor_version_upgrade          = true
  copy_tags_to_snapshot               = true
  iam_database_authentication_enabled = true

  lifecycle {
    ignore_changes = [password]
  }

  tags = merge(local.module_tags, {
    Component = "rds-instance"
    Name      = local.db_identifier
  })
}

# Group A — Secrets Manager

resource "aws_secretsmanager_secret" "db_credentials" {
  # checkov:skip=CKV_AWS_149: AWS Academy no permite KMS CMK; cifrado con clave AWS-owned.
  # checkov:skip=CKV2_AWS_57: Rotación automática deshabilitada; LabRole no puede crear funciones Lambda de rotación (restricción de sandbox).
  name                    = format("%s-db-credentials", var.project)
  recovery_window_in_days = 0

  tags = merge(local.module_tags, {
    Component = "secrets-manager-secret"
    Name      = format("%s-db-credentials", var.project)
    Role      = "db-credentials"
  })
}

resource "aws_secretsmanager_secret_version" "db_credentials" {
  secret_id = aws_secretsmanager_secret.db_credentials.id
  secret_string = jsonencode({
    username = var.db_username
    password = var.db_password
  })
}

# Group B — Proxy Security Group

resource "aws_security_group" "proxy" {
  name        = format("%s-proxy-sg", var.project)
  description = "RDS Proxy; ingress TCP/5432 desde Lambdas; egress TCP/5432 hacia RDS. Las reglas se definen en la composicion raiz para evitar dependencias circulares entre modulos."
  vpc_id      = var.vpc_id

  tags = merge(local.module_tags, {
    Component = "security-group"
    Name      = format("%s-proxy-sg", var.project)
  })
}

# Group C — Proxy Resources

resource "aws_db_proxy" "results" {
  name                   = format("%s-results-proxy", var.project)
  debug_logging          = false
  engine_family          = "POSTGRESQL"
  idle_client_timeout    = 1800
  require_tls            = true
  role_arn               = var.principal_arn
  vpc_security_group_ids = [aws_security_group.proxy.id]
  vpc_subnet_ids         = var.private_subnet_ids

  auth {
    auth_scheme = "SECRETS"
    description = "DB credentials for fraud-results RDS"
    iam_auth    = "DISABLED"
    secret_arn  = aws_secretsmanager_secret.db_credentials.arn
  }

  tags = merge(local.module_tags, {
    Component = "rds-proxy"
    Name      = format("%s-results-proxy", var.project)
  })
}

resource "aws_db_proxy_default_target_group" "results" {
  db_proxy_name = aws_db_proxy.results.name

  connection_pool_config {
    max_connections_percent      = 90
    max_idle_connections_percent = 50
    connection_borrow_timeout    = 120
  }

  lifecycle {
    replace_triggered_by = [aws_db_proxy.results.id]
  }
}

resource "aws_db_proxy_target" "results" {
  db_instance_identifier = aws_db_instance.results.identifier
  db_proxy_name          = aws_db_proxy.results.name
  target_group_name      = aws_db_proxy_default_target_group.results.name

  lifecycle {
    replace_triggered_by = [aws_db_proxy.results.id]
  }
}
