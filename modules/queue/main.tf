locals {
  module_tags = merge(var.tags, {
    Service = "ingestion-queue"
  })

  queue_name = format("%s-transactions", var.project)
  dlq_name   = format("%s-transactions-dlq", var.project)
}

resource "aws_sqs_queue" "dlq" {
  name = local.dlq_name

  message_retention_seconds = var.dlq_message_retention_seconds
  sqs_managed_sse_enabled   = true

  tags = merge(local.module_tags, {
    Component = "sqs-queue"
    Name      = local.dlq_name
    Role      = "dlq"
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main.arn]
  })
}

resource "aws_sqs_queue" "main" {
  name = local.queue_name

  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = merge(local.module_tags, {
    Component = "sqs-queue"
    Name      = local.queue_name
    Role      = "primary"
  })
}

data "aws_iam_policy_document" "main" {
  statement {
    sid    = "AllowLabRolePublishConsume"
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

    resources = [aws_sqs_queue.main.arn]
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.main.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Nota: se intentó restringir SendMessage al CIDR/VPC on-premise via
  # aws:VpcSourceIp y aws:SourceVpc, pero ambas condition keys no se
  # propagan para tráfico cross-VPC via VPN Site-to-Site hacia un Interface
  # VPC Endpoint. La restricción de red queda garantizada arquitecturalmente
  # por el VPN + VPCE; en producción se usarían credenciales IAM dedicadas
  # al sistema on-prem con aws:SourceIp sobre su IP pública fija.
}

resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.id
  policy    = data.aws_iam_policy_document.main.json
}

data "aws_iam_policy_document" "dlq" {
  statement {
    sid    = "AllowLabRoleConsume"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [var.principal_arn]
    }

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]

    resources = [aws_sqs_queue.dlq.arn]
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.dlq.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy    = data.aws_iam_policy_document.dlq.json
}
