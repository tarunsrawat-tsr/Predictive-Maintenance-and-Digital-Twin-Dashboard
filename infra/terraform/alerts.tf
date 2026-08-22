# ------------------------------------------------------------------------------
# Notifications: SNS topic for critical machine alerts + platform alarms
# ------------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  # Deliberately not encrypted with the AWS-managed `aws/sns` key: CloudWatch Alarms and AWS
  # Budgets cannot publish to such topics. Use a customer-managed KMS key (+ key policy) if
  # encryption at rest is a requirement.
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudWatchAlarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
        Condition = { ArnLike = { "aws:SourceArn" = "arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:*" } }
      },
      {
        Sid       = "AllowBudgets"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.alerts.arn
      },
      {
        Sid       = "AllowOwner"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = ["sns:Publish", "sns:Subscribe", "sns:Receive"]
        Resource  = aws_sns_topic.alerts.arn
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
