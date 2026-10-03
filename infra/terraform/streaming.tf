# ------------------------------------------------------------------------------
# Streaming backbone: Kinesis Data Stream (hot path) + Firehose -> S3 data lake (cold path)
# ------------------------------------------------------------------------------

resource "aws_kinesis_stream" "telemetry" {
  name             = "${local.name}-telemetry"
  shard_count      = var.kinesis_shard_count
  retention_period = 24
  encryption_type  = "KMS"
  kms_key_id       = "alias/aws/kinesis"

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  shard_level_metrics = ["IncomingRecords", "IteratorAgeMilliseconds"]
}

# ---- data lake bucket (raw + scored telemetry, newline-delimited JSON, gzip, Hive partitions)
resource "aws_s3_bucket" "datalake" {
  bucket        = "${local.name}-${local.account_id}-datalake"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "datalake" {
  bucket                  = aws_s3_bucket.datalake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "datalake" {
  bucket = aws_s3_bucket.datalake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "datalake" {
  bucket = aws_s3_bucket.datalake.id

  rule {
    id     = "tier-and-expire"
    status = "Enabled"

    filter {
      prefix = "telemetry/"
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    dynamic "expiration" {
      for_each = var.datalake_expiration_days > 0 ? [1] : []
      content {
        days = var.datalake_expiration_days
      }
    }
  }
}

# ---- Firehose (direct put from the scorer, so the archive contains predictions too)
resource "aws_cloudwatch_log_group" "firehose" {
  name              = "/aws/kinesisfirehose/${local.name}-archive"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_stream" "firehose" {
  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose.name
}

resource "aws_iam_role" "firehose" {
  name = "${local.name}-firehose"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "sts:ExternalId" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "firehose" {
  name = "s3-and-logs"
  role = aws_iam_role.firehose.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:GetObject",
          "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutObject"
        ]
        Resource = [aws_s3_bucket.datalake.arn, "${aws_s3_bucket.datalake.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.firehose.arn}:*"
      }
    ]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "archive" {
  name        = "${local.name}-archive"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.datalake.arn
    prefix              = "telemetry/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    buffering_interval  = 60
    buffering_size      = 5
    compression_format  = "GZIP"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose.name
      log_stream_name = aws_cloudwatch_log_stream.firehose.name
    }
  }
}

# ---- Athena over the data lake (ad-hoc analytics, model monitoring)
resource "aws_glue_catalog_database" "datalake" {
  name = replace("${local.name}_datalake", "-", "_")
}

resource "aws_glue_catalog_table" "telemetry" {
  name          = "telemetry"
  database_name = aws_glue_catalog_database.datalake.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    classification              = "json"
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "2024,2035"
    "projection.month.type"     = "integer"
    "projection.month.range"    = "1,12"
    "projection.month.digits"   = "2"
    "projection.day.type"       = "integer"
    "projection.day.range"      = "1,31"
    "projection.day.digits"     = "2"
    "storage.location.template" = "s3://${aws_s3_bucket.datalake.bucket}/telemetry/year=$${year}/month=$${month}/day=$${day}/"
  }

  partition_keys {
    name = "year"
    type = "int"
  }
  partition_keys {
    name = "month"
    type = "int"
  }
  partition_keys {
    name = "day"
    type = "int"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.datalake.bucket}/telemetry/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "true"
      }
    }

    columns {
      name = "machine_id"
      type = "string"
    }
    columns {
      name = "ts"
      type = "bigint"
    }
    columns {
      name = "ts_iso"
      type = "string"
    }
    columns {
      name = "site"
      type = "string"
    }
    columns {
      name = "line"
      type = "string"
    }
    columns {
      name = "cycle"
      type = "int"
    }
    columns {
      name = "settings"
      type = "map<string,double>"
    }
    columns {
      name = "sensors"
      type = "map<string,double>"
    }
    columns {
      name = "rul_p10"
      type = "double"
    }
    columns {
      name = "rul_p50"
      type = "double"
    }
    columns {
      name = "rul_p90"
      type = "double"
    }
    columns {
      name = "anomaly_score"
      type = "double"
    }
    columns {
      name = "anomaly_raw"
      type = "double"
    }
    columns {
      name = "anomaly_ready"
      type = "boolean"
    }
    columns {
      name = "health_index"
      type = "double"
    }
    columns {
      name = "status"
      type = "string"
    }
    columns {
      name = "driver"
      type = "string"
    }
  }
}
