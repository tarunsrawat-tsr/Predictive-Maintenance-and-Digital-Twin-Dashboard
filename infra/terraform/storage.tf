# ------------------------------------------------------------------------------
# Serving store (DynamoDB), model artifacts (S3), container registries (ECR)
# ------------------------------------------------------------------------------
#
# Why DynamoDB and not Amazon Timestream?  Timestream for LiveAnalytics closed to new AWS
# customers on 2025-06-20 and its successor (Timestream for InfluxDB) is an instance inside a
# VPC. The dashboard's access patterns -- "latest N points for one machine" and "current state
# of every machine" -- map directly onto a partition/sort-key design, which keeps the hot path
# serverless and essentially free at demo scale. Analytical history goes to S3 + Athena.

resource "aws_dynamodb_table" "telemetry" {
  name         = "${local.name}-telemetry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "machine_id"
  range_key    = "ts"

  attribute {
    name = "machine_id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_dynamodb_table" "machine_state" {
  name         = "${local.name}-machine-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "machine_id"

  attribute {
    name = "machine_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "alerts" {
  name         = "${local.name}-alerts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "machine_id"
  range_key    = "ts"

  attribute {
    name = "machine_id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "N"
  }
  attribute {
    name = "status"
    type = "S"
  }

  global_secondary_index {
    name            = "status-ts-index"
    hash_key        = "status"
    range_key       = "ts"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ---- model artifacts
resource "aws_s3_bucket" "models" {
  bucket        = "${local.name}-${local.account_id}-models"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "models" {
  bucket = aws_s3_bucket.models.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "models" {
  bucket                  = aws_s3_bucket.models.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "models" {
  bucket = aws_s3_bucket.models.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---- container registries
locals {
  ecr_repos = toset(["scorer", "dashboard", "simulator"])
}

resource "aws_ecr_repository" "svc" {
  for_each             = local.ecr_repos
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "svc" {
  for_each   = aws_ecr_repository.svc
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 10 images"
      selection    = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

locals {
  model_s3_uri = var.model_s3_prefix != "" ? "s3://${aws_s3_bucket.models.bucket}/${trim(var.model_s3_prefix, "/")}" : ""
}
