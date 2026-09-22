# ------------------------------------------------------------------------------
# AWS IoT Core: device identity (X.509), least-privilege policy, topic rule -> Kinesis
# ------------------------------------------------------------------------------

data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}

resource "aws_iot_thing" "gateway" {
  name = "${local.name}-gateway"

  attributes = {
    site = var.simulator_site
    role = "edge-gateway"
  }
}

# Terraform asks IoT Core to generate the key pair + certificate. The private key only ever
# lives in state and in an SSM SecureString (consumed by the Fargate simulator task).
resource "aws_iot_certificate" "gateway" {
  active = true
}

resource "aws_iot_policy" "gateway" {
  name = "${local.name}-gateway-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iot:Connect"]
        Resource = "arn:aws:iot:${local.region}:${local.account_id}:client/${var.mqtt_client_id}*"
      },
      {
        Effect   = "Allow"
        Action   = ["iot:Publish"]
        Resource = "arn:aws:iot:${local.region}:${local.account_id}:topic/factory/*"
      }
    ]
  })
}

resource "aws_iot_policy_attachment" "gateway" {
  policy = aws_iot_policy.gateway.name
  target = aws_iot_certificate.gateway.arn
}

resource "aws_iot_thing_principal_attachment" "gateway" {
  principal = aws_iot_certificate.gateway.arn
  thing     = aws_iot_thing.gateway.name
}

# ---- credentials for the simulator / local runs (SecureString)
resource "aws_ssm_parameter" "iot_cert_pem" {
  name  = "/${local.name}/iot/gateway/certificate_pem"
  type  = "SecureString"
  value = aws_iot_certificate.gateway.certificate_pem
}

resource "aws_ssm_parameter" "iot_private_key" {
  name  = "/${local.name}/iot/gateway/private_key"
  type  = "SecureString"
  value = aws_iot_certificate.gateway.private_key
}

# ---- topic rule: every telemetry message -> Kinesis, partitioned by machine
resource "aws_cloudwatch_log_group" "iot_rule_errors" {
  name              = "/aws/iot/${local.name}-rule-errors"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "iot_rule" {
  name = "${local.name}-iot-rule"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "iot.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "iot_rule" {
  name = "kinesis-and-logs"
  role = aws_iam_role.iot_rule.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kinesis:PutRecord", "kinesis:PutRecords"]
        Resource = aws_kinesis_stream.telemetry.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.iot_rule_errors.arn}:*"
      }
    ]
  })
}

resource "aws_iot_topic_rule" "telemetry_to_kinesis" {
  name        = replace("${local.name}_telemetry_to_kinesis", "-", "_")
  description = "Fan all factory/<site>/<line>/<machine>/telemetry messages into the Kinesis stream."
  enabled     = true
  sql         = "SELECT *, topic(4) AS topic_machine_id, timestamp() AS ingested_at FROM 'factory/+/+/+/telemetry'"
  sql_version = "2016-03-23"

  kinesis {
    role_arn      = aws_iam_role.iot_rule.arn
    stream_name   = aws_kinesis_stream.telemetry.name
    partition_key = "$${machine_id}"
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.iot_rule_errors.name
      role_arn       = aws_iam_role.iot_rule.arn
    }
  }
}
