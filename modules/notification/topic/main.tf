locals {
  module_tags = merge(var.tags, {
    Service = "notification-topic"
  })

  topic_name = format("%s-fraud-summaries", var.project)

  summary_notification_actions = [
    "sns:GetTopicAttributes",
    "sns:ListSubscriptionsByTopic",
    "sns:Publish",
    "sns:Subscribe",
  ]

  topic_scoped_actions = [
    "sns:AddPermission",
    "sns:DeleteTopic",
    "sns:GetTopicAttributes",
    "sns:ListSubscriptionsByTopic",
    "sns:Publish",
    "sns:RemovePermission",
    "sns:SetTopicAttributes",
    "sns:Subscribe",
  ]
}

resource "aws_sns_topic" "summary" {
  # checkov:skip=CKV_AWS_26: AWS Academy no permite KMS CMK; mensajes viajan por TLS y el alcance es académico.
  name = local.topic_name

  tags = merge(local.module_tags, {
    Component = "sns-topic"
    Name      = local.topic_name
  })
}

data "aws_iam_policy_document" "summary" {
  statement {
    sid    = "AllowLabRoleSummaryNotifications"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [var.principal_arn]
    }

    actions   = local.summary_notification_actions
    resources = [aws_sns_topic.summary.arn]
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = local.topic_scoped_actions
    resources = [aws_sns_topic.summary.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sns_topic_policy" "summary" {
  arn    = aws_sns_topic.summary.arn
  policy = data.aws_iam_policy_document.summary.json
}
