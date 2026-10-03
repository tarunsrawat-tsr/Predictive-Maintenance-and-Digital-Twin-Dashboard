# ------------------------------------------------------------------------------
# Digital-twin console: ALB -> ECS Fargate (Streamlit)
# ------------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# ---- IAM: execution role (pull image, write logs, read secrets) and task roles
data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "read-ssm-secrets"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ssm:GetParameters", "ssm:GetParameter"]
        Resource = concat(
          [aws_ssm_parameter.iot_cert_pem.arn, aws_ssm_parameter.iot_private_key.arn],
          var.dashboard_password != "" ? [aws_ssm_parameter.dashboard_password[0].arn] : []
        )
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = data.aws_kms_alias.ssm.target_key_arn
      }
    ]
  })
}

resource "aws_iam_role" "dashboard_task" {
  name               = "${local.name}-dashboard-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "dashboard_task" {
  name = "read-serving-store"
  role = aws_iam_role.dashboard_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
        Resource = [
          aws_dynamodb_table.telemetry.arn,
          aws_dynamodb_table.machine_state.arn,
          aws_dynamodb_table.alerts.arn,
          "${aws_dynamodb_table.alerts.arn}/index/*"
        ]
      },
      {
        Sid      = "AcknowledgeAlerts"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.alerts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.models.arn, "${aws_s3_bucket.models.arn}/*"]
      }
    ]
  })
}

resource "aws_ssm_parameter" "dashboard_password" {
  count = var.dashboard_password != "" ? 1 : 0
  name  = "/${local.name}/dashboard/password"
  type  = "SecureString"
  value = var.dashboard_password
}

# ---- load balancer
resource "aws_lb" "dashboard" {
  name               = substr("${local.name}-dashboard", 0, 32)
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  idle_timeout       = 300 # Streamlit keeps a websocket open
}

resource "aws_lb_target_group" "dashboard" {
  name        = substr("${local.name}-dashboard", 0, 32)
  port        = 8501
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/_stcore/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  stickiness {
    type            = "lb_cookie"
    cookie_duration = 86400
    enabled         = true
  }

  deregistration_delay = 15
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.dashboard.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.acm_certificate_arn == "" ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.dashboard.arn
    }
  }

  dynamic "default_action" {
    for_each = var.acm_certificate_arn != "" ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.acm_certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.dashboard.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.dashboard.arn
  }
}

# ---- task + service
resource "aws_cloudwatch_log_group" "dashboard" {
  name              = "/ecs/${local.name}-dashboard"
  retention_in_days = var.log_retention_days
}

locals {
  dashboard_env = merge(
    {
      PDM_BACKEND                   = "dynamodb"
      AWS_REGION                    = local.region
      PDM_TELEMETRY_TABLE           = aws_dynamodb_table.telemetry.name
      PDM_MACHINE_STATE_TABLE       = aws_dynamodb_table.machine_state.name
      PDM_ALERTS_TABLE              = aws_dynamodb_table.alerts.name
      PDM_MODEL_S3_URI              = local.model_s3_uri
      PDM_DASHBOARD_REFRESH_SECONDS = tostring(var.dashboard_refresh_seconds)
    },
    { for k, v in local.scorer_env : k => v if startswith(k, "PDM_RUL_") || startswith(k, "PDM_ANOMALY_") || startswith(k, "PDM_SAFETY") || startswith(k, "PDM_HYSTERESIS") || startswith(k, "PDM_CYCLE") || startswith(k, "PDM_DOWNTIME") || startswith(k, "PDM_UNPLANNED") || startswith(k, "PDM_PLANNED") || startswith(k, "PDM_MAINTENANCE") }
  )
}

resource "aws_ecs_task_definition" "dashboard" {
  family                   = "${local.name}-dashboard"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.dashboard_cpu
  memory                   = var.dashboard_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.dashboard_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "dashboard"
      image     = "${aws_ecr_repository.svc["dashboard"].repository_url}:${var.image_tag}"
      essential = true
      portMappings = [{
        containerPort = 8501
        protocol      = "tcp"
      }]
      environment = [for k, v in local.dashboard_env : { name = k, value = v }]
      secrets = var.dashboard_password != "" ? [{
        name      = "DASHBOARD_PASSWORD"
        valueFrom = aws_ssm_parameter.dashboard_password[0].arn
      }] : []
      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://localhost:8501/_stcore/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.dashboard.name
          awslogs-region        = local.region
          awslogs-stream-prefix = "dashboard"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "dashboard" {
  name                              = "${local.name}-dashboard"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.dashboard.arn
  desired_count                     = var.dashboard_desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 90

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.dashboard.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.dashboard.arn
    container_name   = "dashboard"
    container_port   = 8501
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http]
}
