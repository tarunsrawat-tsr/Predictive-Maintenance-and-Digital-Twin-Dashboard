# ------------------------------------------------------------------------------
# Fleet simulator on Fargate: replays NASA C-MAPSS over MQTT/TLS into IoT Core.
# Set simulator_desired_count = 0 to run it from a laptop instead (see `make simulate`).
# ------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "simulator" {
  name              = "/ecs/${local.name}-simulator"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "simulator_task" {
  name               = "${local.name}-simulator-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_ecs_task_definition" "simulator" {
  family                   = "${local.name}-simulator"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.simulator_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "simulator"
      image     = "${aws_ecr_repository.svc["simulator"].repository_url}:${var.image_tag}"
      essential = true
      environment = [
        { name = "MQTT_HOST", value = data.aws_iot_endpoint.data.endpoint_address },
        { name = "MQTT_PORT", value = "8883" },
        { name = "MQTT_CLIENT_ID", value = var.mqtt_client_id },
        { name = "SIM_MACHINES", value = tostring(var.simulator_machines) },
        { name = "SIM_INTERVAL", value = tostring(var.simulator_interval_seconds) },
        { name = "SIM_SITE", value = var.simulator_site },
        { name = "SIM_DATASET", value = "FD001" },
        { name = "SIM_SPLIT", value = "train" },
      ]
      secrets = [
        { name = "IOT_CERT_PEM", valueFrom = aws_ssm_parameter.iot_cert_pem.arn },
        { name = "IOT_PRIVATE_KEY_PEM", valueFrom = aws_ssm_parameter.iot_private_key.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.simulator.name
          awslogs-region        = local.region
          awslogs-stream-prefix = "simulator"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "simulator" {
  name            = "${local.name}-simulator"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.simulator.arn
  desired_count   = var.simulator_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.simulator.id]
    assign_public_ip = true
  }

  # The simulator should only start once the scorer can consume the stream.
  depends_on = [aws_lambda_event_source_mapping.kinesis, aws_iot_topic_rule.telemetry_to_kinesis]
}
