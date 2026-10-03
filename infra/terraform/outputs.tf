output "dashboard_url" {
  description = "Digital-twin console."
  value       = var.acm_certificate_arn != "" ? "https://${aws_lb.dashboard.dns_name}" : "http://${aws_lb.dashboard.dns_name}"
}

output "iot_endpoint" {
  description = "MQTT/TLS endpoint for devices and the simulator."
  value       = data.aws_iot_endpoint.data.endpoint_address
}

output "iot_topic_filter" {
  value = "factory/+/+/+/telemetry"
}

output "iot_cert_ssm_parameters" {
  description = "SSM SecureString parameters holding the gateway certificate and private key."
  value       = {
    certificate_pem = aws_ssm_parameter.iot_cert_pem.name
    private_key     = aws_ssm_parameter.iot_private_key.name
  }
}

output "ecr_repositories" {
  description = "Push targets for `make push`."
  value       = { for k, r in aws_ecr_repository.svc : k => r.repository_url }
}

output "kinesis_stream_name" {
  value = aws_kinesis_stream.telemetry.name
}

output "firehose_stream_name" {
  value = aws_kinesis_firehose_delivery_stream.archive.name
}

output "dynamodb_tables" {
  value = {
    telemetry     = aws_dynamodb_table.telemetry.name
    machine_state = aws_dynamodb_table.machine_state.name
    alerts        = aws_dynamodb_table.alerts.name
  }
}

output "model_bucket" {
  value = aws_s3_bucket.models.bucket
}

output "datalake_bucket" {
  value = aws_s3_bucket.datalake.bucket
}

output "athena_database" {
  value = aws_glue_catalog_database.datalake.name
}

output "sns_alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "scorer_function_name" {
  value = aws_lambda_function.scorer.function_name
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "cloudwatch_dashboard" {
  value = "https://${local.region}.console.aws.amazon.com/cloudwatch/home?region=${local.region}#dashboards:name=${aws_cloudwatch_dashboard.platform.dashboard_name}"
}

output "aws_region" {
  value = local.region
}
