locals {
  module_tags = merge(var.tags, {
    Service = "results-writer"
  })

  queue_name     = format("%s-results-events", var.project)
  dlq_name       = format("%s-results-events-dlq", var.project)
  function_name  = format("%s-results-writer", var.project)
  log_group_name = format("/aws/lambda/%s-results-writer", var.project)
}

resource "aws_sqs_queue" "results_dlq" {
  name = local.dlq_name

  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  tags = merge(local.module_tags, {
    Component = "sqs-queue"
    Name      = local.dlq_name
    Role      = "dlq"
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "results_dlq" {
  queue_url = aws_sqs_queue.results_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.results.arn]
  })
}

resource "aws_sqs_queue" "results" {
  name = local.queue_name

  # Visibility timeout >= 6× Lambda timeout (Lambda timeout = 60s).
  visibility_timeout_seconds = 360
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.results_dlq.arn
    maxReceiveCount     = 3
  })

  tags = merge(local.module_tags, {
    Component = "sqs-queue"
    Name      = local.queue_name
    Role      = "primary"
  })
}

data "aws_iam_policy_document" "results_queue" {
  statement {
    sid    = "AllowLabRoleSendAndConsume"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [var.principal_arn]
    }

    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ChangeMessageVisibility",
    ]

    resources = [aws_sqs_queue.results.arn]
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.results.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "results" {
  queue_url = aws_sqs_queue.results.id
  policy    = data.aws_iam_policy_document.results_queue.json
}

resource "aws_cloudwatch_log_group" "writer" {
  # checkov:skip=CKV_AWS_158: AWS Academy no permite KMS CMK; cifrado con clave AWS-owned.
  # checkov:skip=CKV_AWS_338: retención corta acorde al alcance académico.
  name              = local.log_group_name
  retention_in_days = var.log_retention_days

  tags = merge(local.module_tags, {
    Component = "cloudwatch-log-group"
    Name      = local.log_group_name
  })
}

resource "aws_security_group" "writer_lambda" {
  name        = format("%s-writer-lambda-sg", var.project)
  description = "Lambda results-writer; no ingress; egress to VPC endpoints (tcp/443) and RDS (tcp/5432). Cross-module SG rules are managed in the root composition."
  vpc_id      = var.vpc_id

  tags = merge(local.module_tags, {
    Component = "security-group"
    Name      = format("%s-writer-lambda-sg", var.project)
  })
}

resource "aws_vpc_security_group_egress_rule" "writer_to_endpoints" {
  security_group_id            = aws_security_group.writer_lambda.id
  description                  = "HTTPS hacia los Interface VPC Endpoints (Logs, SQS, SNS)"
  referenced_security_group_id = var.endpoint_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

resource "aws_lambda_function" "writer" {
  # checkov:skip=CKV_AWS_173: AWS Academy no expone alias/aws/lambda; env vars usan cifrado por defecto del servicio.
  # checkov:skip=CKV_AWS_272: Code signing no configurado en lab académico.
  # checkov:skip=CKV_AWS_50: X-Ray tracing deshabilitado en lab.
  # checkov:skip=CKV_AWS_116: DLQ a nivel Lambda no necesario; se usa el DLQ de la cola SQS.
  # checkov:skip=CKV_AWS_117: Lambda desplegada en VPC para acceder a RDS en subnets privadas.
  function_name                  = local.function_name
  role                           = var.principal_arn
  runtime                        = "provided.al2023"
  handler                        = "bootstrap"
  timeout                        = 60
  memory_size                    = 1024
  architectures                  = ["x86_64"]
  reserved_concurrent_executions = 5 # AWS Academy account cap is 10 concurrent Lambdas total

  filename         = var.package_file
  source_code_hash = filebase64sha256(var.package_file)

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.writer_lambda.id]
  }

  environment {
    variables = {
      DB_HOST     = var.db_host
      DB_PORT     = tostring(var.db_port)
      DB_NAME     = var.db_name
      DB_USER     = var.db_username
      DB_PASSWORD = var.db_password
    }
  }

  depends_on = [aws_cloudwatch_log_group.writer]

  tags = merge(local.module_tags, {
    Component = "lambda"
    Name      = local.function_name
  })
}

resource "aws_lambda_event_source_mapping" "sqs_results" {
  event_source_arn                   = aws_sqs_queue.results.arn
  function_name                      = aws_lambda_function.writer.arn
  batch_size                         = var.sqs_batch_size
  maximum_batching_window_in_seconds = var.sqs_batching_window_seconds
  enabled                            = true
}
