variable "project" {
  description = "Project slug used as a prefix for every resource name."
  type        = string
  default     = "pdm"
}

variable "environment" {
  description = "Deployment environment (dev/stg/prd)."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region. ap-northeast-1 (Tokyo) by default for a Japan-based plant."
  type        = string
  default     = "ap-northeast-1"
}

# ------------------------------------------------------------------ images / model
variable "image_tag" {
  description = "Tag of the container images in ECR (scorer, dashboard, simulator). `make push` tags with the git SHA."
  type        = string
  default     = "latest"
}

variable "model_s3_prefix" {
  description = "If set (e.g. \"models/rul\"), the scorer and dashboard load the model bundle from the model bucket under this prefix instead of the copy baked into the image. Upload with `make upload-model`."
  type        = string
  default     = ""
}

# ------------------------------------------------------------------ streaming
variable "kinesis_shard_count" {
  description = "Provisioned shards for the telemetry stream (1 shard = 1 MB/s or 1000 rec/s ingest)."
  type        = number
  default     = 1
}

variable "lambda_batch_size" {
  description = "Max records per scorer invocation."
  type        = number
  default     = 200
}

variable "lambda_batch_window_seconds" {
  description = "Max seconds the event source mapping buffers records before invoking the scorer."
  type        = number
  default     = 5
}

variable "lambda_memory_mb" {
  type    = number
  default = 1024
}

variable "telemetry_ttl_days" {
  description = "Days of hot telemetry kept in DynamoDB (long-term history lives in S3)."
  type        = number
  default     = 7
}

variable "datalake_expiration_days" {
  description = "Days before archived telemetry objects in S3 expire (0 = never)."
  type        = number
  default     = 365
}

# ------------------------------------------------------------------ business rules (scorer env)
variable "health_thresholds" {
  description = "Business thresholds passed to the scorer as environment variables."
  type = object({
    rul_cap            = optional(number, 125)
    rul_warning        = optional(number, 50)
    rul_critical       = optional(number, 20)
    anomaly_warning    = optional(number, 1.0)
    anomaly_critical   = optional(number, 2.0)
    safety_margin      = optional(number, 10)
    hysteresis_rul     = optional(number, 8)
    hysteresis_anomaly = optional(number, 0.25)
    anomaly_ewma_alpha = optional(number, 0.3)
    cycle_hours        = optional(number, 8)
    downtime_cost_hour = optional(number, 15000)
    unplanned_outage_h = optional(number, 24)
    planned_outage_h   = optional(number, 6)
    maintenance_crews  = optional(number, 2)
  })
  default = {}
}

# ------------------------------------------------------------------ alerts
variable "alert_email" {
  description = "Email address subscribed to critical alerts and ops alarms (leave empty to skip). Must be confirmed via the SNS email."
  type        = string
  default     = ""
}

# ------------------------------------------------------------------ dashboard
variable "dashboard_desired_count" {
  description = "Number of dashboard tasks."
  type        = number
  default     = 1
}

variable "dashboard_cpu" {
  type    = number
  default = 512
}

variable "dashboard_memory" {
  type    = number
  default = 1024
}

variable "dashboard_allowed_cidrs" {
  description = "CIDR blocks allowed to reach the dashboard load balancer. Restrict to your office/VPN."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "dashboard_password" {
  description = "Optional shared access code for the dashboard (stored in SSM SecureString). For production put Cognito/OIDC on the ALB instead."
  type        = string
  default     = ""
  sensitive   = true
}

variable "acm_certificate_arn" {
  description = "Optional ACM certificate ARN. When set the ALB serves HTTPS and redirects HTTP."
  type        = string
  default     = ""
}

variable "dashboard_refresh_seconds" {
  type    = number
  default = 5
}

# ------------------------------------------------------------------ simulator
variable "simulator_desired_count" {
  description = "0 to stop the cloud-hosted simulator (run it from a laptop instead), 1 to run it on Fargate."
  type        = number
  default     = 1
}

variable "simulator_machines" {
  type    = number
  default = 10
}

variable "simulator_interval_seconds" {
  description = "Wall-clock seconds per machine cycle. 5s with 10 machines = 2 msg/s ≈ 5M DynamoDB writes/month."
  type        = number
  default     = 5
}

variable "simulator_site" {
  type    = string
  default = "nagoya"
}

variable "mqtt_client_id" {
  description = "MQTT client id the gateway certificate is allowed to connect with."
  type        = string
  default     = "pdm-gateway"
}

# ------------------------------------------------------------------ cost guardrails
variable "monthly_budget_usd" {
  description = "AWS Budgets alert threshold for this account/project (0 disables)."
  type        = number
  default     = 100
}

variable "log_retention_days" {
  type    = number
  default = 14
}
