# ------------------------------------------------------------------------------
# Observability + cost guardrails
# ------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "scorer_errors" {
  alarm_name          = "${local.name}-scorer-errors"
  alarm_description   = "Scorer Lambda is failing invocations."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.scorer.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "scorer_iterator_age" {
  alarm_name          = "${local.name}-scorer-iterator-age"
  alarm_description   = "Scorer is falling behind the stream (> 2 min of lag)."
  namespace           = "AWS/Lambda"
  metric_name         = "IteratorAge"
  dimensions          = { FunctionName = aws_lambda_function.scorer.function_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 120000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "scorer_dlq" {
  alarm_name          = "${local.name}-scorer-dlq-messages"
  alarm_description   = "Batches landed in the scorer dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.scorer_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "no_ingest" {
  alarm_name          = "${local.name}-no-telemetry"
  alarm_description   = "No telemetry has reached Kinesis for 15 minutes (gateway/simulator down?)."
  namespace           = "AWS/Kinesis"
  metric_name         = "IncomingRecords"
  dimensions          = { StreamName = aws_kinesis_stream.telemetry.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

locals {
  cw_widgets = [
    {
      title   = "Ingest (Kinesis records / 1 min)"
      stat    = "Sum"
      metrics = [["AWS/Kinesis", "IncomingRecords", "StreamName", aws_kinesis_stream.telemetry.name]]
    },
    {
      title = "Scorer invocations / errors"
      stat  = "Sum"
      metrics = [
        ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.scorer.function_name],
        [".", "Errors", ".", "."]
      ]
    },
    {
      title = "Scorer latency p50 / p95 (ms)"
      stat  = "p50"
      metrics = [
        ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.scorer.function_name, { stat = "p50" }],
        ["...", { stat = "p95" }]
      ]
    },
    {
      title   = "Stream lag (IteratorAge ms)"
      stat    = "Maximum"
      metrics = [["AWS/Lambda", "IteratorAge", "FunctionName", aws_lambda_function.scorer.function_name]]
    },
    {
      title = "DynamoDB write units"
      stat  = "Sum"
      metrics = [
        ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", aws_dynamodb_table.telemetry.name],
        ["...", aws_dynamodb_table.machine_state.name],
        ["...", aws_dynamodb_table.alerts.name]
      ]
    },
    {
      title = "Dashboard (ECS) CPU / memory %"
      stat  = "Average"
      metrics = [
        ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.dashboard.name],
        [".", "MemoryUtilization", ".", ".", ".", "."]
      ]
    },
  ]
}

resource "aws_cloudwatch_dashboard" "platform" {
  dashboard_name = "${local.name}-platform"

  dashboard_body = jsonencode({
    widgets = [
      for i, w in local.cw_widgets : {
        type   = "metric"
        x      = (i % 3) * 8
        y      = floor(i / 3) * 6
        width  = 8
        height = 6
        properties = {
          title   = w.title
          region  = local.region
          stat    = w.stat
          period  = 60
          view    = "timeSeries"
          metrics = w.metrics
        }
      }
    ]
  })
}

resource "aws_budgets_budget" "monthly" {
  count        = var.monthly_budget_usd > 0 ? 1 : 0
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Account-wide on purpose: tag-based cost filters only work after the `Project` cost
  # allocation tag has been activated in Billing (takes ~24h). Narrow it down later if needed.

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
    subscriber_email_addresses = var.alert_email != "" ? [var.alert_email] : []
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
    subscriber_email_addresses = var.alert_email != "" ? [var.alert_email] : []
  }
}
