locals {
  module_tags = merge(var.tags, {
    Service = "notification-summary-queue"
  })

  queue_name = format("%s-fraud-alerts", var.project)
  dlq_name   = format("%s-fraud-alerts-dlq", var.project)
}

resource "aws_sqs_queue" "dlq" {
  name = local.dlq_name

  message_retention_seconds = 1209600
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

  visibility_timeout_seconds = 180
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })

  tags = merge(local.module_tags, {
    Component = "sqs-queue"
    Name      = local.queue_name
    Role      = "primary"
  })
}

data "aws_iam_policy_document" "main" {
  statement {
    sid    = "AllowLabRoleSendAndConsume"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [var.principal_arn]
    }

    actions = [
      "sqs:ChangeMessageVisibility",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ReceiveMessage",
      "sqs:SendMessage",
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
}

resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.id
  policy    = data.aws_iam_policy_document.main.json
}
