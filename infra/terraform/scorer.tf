# ------------------------------------------------------------------------------
# Streaming scorer: Kinesis -> Lambda (container image) -> DynamoDB / Firehose / SNS
# ------------------------------------------------------------------------------

locals {
  th = var.health_thresholds

  scorer_env = {
    PDM_BACKEND                = "dynamodb"
    PDM_TELEMETRY_TABLE        = aws_dynamodb_table.telemetry.name
    PDM_MACHINE_STATE_TABLE    = aws_dynamodb_table.machine_state.name
    PDM_ALERTS_TABLE           = aws_dynamodb_table.alerts.name
    PDM_TELEMETRY_TTL_DAYS     = tostring(var.telemetry_ttl_days)
    PDM_MODEL_S3_URI           = local.model_s3_uri
    PDM_FIREHOSE_STREAM        = aws_kinesis_firehose_delivery_stream.archive.name
    PDM_SNS_TOPIC_ARN          = aws_sns_topic.alerts.arn
    PDM_RUL_CAP                = tostring(local.th.rul_cap)
    PDM_RUL_WARNING            = tostring(local.th.rul_warning)
    PDM_RUL_CRITICAL           = tostring(local.th.rul_critical)
    PDM_ANOMALY_WARNING        = tostring(local.th.anomaly_warning)
    PDM_ANOMALY_CRITICAL       = tostring(local.th.anomaly_critical)
    PDM_SAFETY_MARGIN_CYCLES   = tostring(local.th.safety_margin)
    PDM_HYSTERESIS_RUL         = tostring(local.th.hysteresis_rul)
    PDM_HYSTERESIS_ANOMALY     = tostring(local.th.hysteresis_anomaly)
    PDM_ANOMALY_EWMA_ALPHA     = tostring(local.th.anomaly_ewma_alpha)
    PDM_CYCLE_HOURS            = tostring(local.th.cycle_hours)
    PDM_DOWNTIME_COST_PER_HOUR = tostring(local.th.downtime_cost_hour)
    PDM_UNPLANNED_OUTAGE_HOURS = tostring(local.th.unplanned_outage_h)
    PDM_PLANNED_OUTAGE_HOURS   = tostring(local.th.planned_outage_h)
    PDM_MAINTENANCE_CREWS      = tostring(local.th.maintenance_crews)
    LOG_LEVEL                  = "INFO"
  }
}

resource "aws_cloudwatch_log_group" "scorer" {
  name              = "/aws/lambda/${local.name}-scorer"
  retention_in_days = var.log_retention_days
}

resource "aws_sqs_queue" "scorer_dlq" {
  name                      = "${local.name}-scorer-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_iam_role" "scorer" {
  name = "${local.name}-scorer"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "scorer_basic" {
  role       = aws_iam_role.scorer.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "scorer" {
  name = "pipeline-access"
  role = aws_iam_role.scorer.id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadStream"
        Effect = "Allow"
        Action = [
          "kinesis:DescribeStream", "kinesis:DescribeStreamSummary", "kinesis:GetRecords",
          "kinesis:GetShardIterator", "kinesis:ListShards", "kinesis:ListStreams", "kinesis:SubscribeToShard"
        ]
        Resource = aws_kinesis_stream.telemetry.arn
      },
      {
        Sid    = "ServingStore"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
          "dynamodb:Scan", "dynamodb:BatchWriteItem"
        ]
        Resource = [
          aws_dynamodb_table.telemetry.arn,
          aws_dynamodb_table.machine_state.arn,
          aws_dynamodb_table.alerts.arn,
          "${aws_dynamodb_table.alerts.arn}/index/*"
        ]
      },
      {
        Sid      = "ModelArtifacts"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.models.arn, "${aws_s3_bucket.models.arn}/*"]
      },
      {
        Sid      = "Archive"
        Effect   = "Allow"
        Action   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
        Resource = aws_kinesis_firehose_delivery_stream.archive.arn
      },
      {
        Sid      = "Notify"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Sid      = "DeadLetter"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.scorer_dlq.arn
      }
    ]
  })
}

resource "aws_lambda_function" "scorer" {
  function_name = "${local.name}-scorer"
  description   = "Scores telemetry micro-batches: RUL + anomaly + health, writes DynamoDB/S3, raises alerts."
  role          = aws_iam_role.scorer.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.svc["scorer"].repository_url}:${var.image_tag}"
  architectures = ["x86_64"]
  memory_size   = var.lambda_memory_mb
  timeout       = 60

  # One shard -> one concurrent invocation per shard; cap to protect DynamoDB on redeploys.
  reserved_concurrent_executions = var.kinesis_shard_count * 2

  environment {
    variables = local.scorer_env
  }

  depends_on = [
    aws_cloudwatch_log_group.scorer,
    aws_iam_role_policy_attachment.scorer_basic,
    aws_iam_role_policy.scorer,
  ]
}

resource "aws_lambda_event_source_mapping" "kinesis" {
  event_source_arn                   = aws_kinesis_stream.telemetry.arn
  function_name                      = aws_lambda_function.scorer.arn
  starting_position                  = "LATEST"
  batch_size                         = var.lambda_batch_size
  maximum_batching_window_in_seconds = var.lambda_batch_window_seconds
  parallelization_factor             = 1
  bisect_batch_on_function_error     = true
  maximum_retry_attempts             = 3
  maximum_record_age_in_seconds      = 3600

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.scorer_dlq.arn
    }
  }
}
