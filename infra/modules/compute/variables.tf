variable "name_prefix" {
  description = "Prefix for every resource name (also the ECS cluster name)."
  type        = string
}

variable "region" {
  description = "AWS region, passed to the application and the collector."
  type        = string
}

# --- network and load balancer ---------------------------------------------------------------

variable "task_subnet_ids" {
  description = "Subnets ECS tasks run in."
  type        = list(string)
}

variable "assign_public_ip" {
  description = "Give tasks a public IP (needed to reach ECR and AWS APIs when there is no NAT gateway)."
  type        = bool
}

variable "tasks_security_group_id" {
  description = "Security group shared by all tasks."
  type        = string
}

variable "services_enabled" {
  description = "Create the api, worker and web services and the worker autoscaling. False puts the environment to sleep: only the cluster, task definitions, ECR and logs remain."
  type        = bool
  default     = true
}

variable "api_target_group_arn" {
  description = "ALB target group of the API (null while services_enabled is false)."
  type        = string
  default     = null
}

variable "web_target_group_arn" {
  description = "ALB target group of the web tier (null while services_enabled is false)."
  type        = string
  default     = null
}

variable "api_upstream" {
  description = "The web tier's API_UPSTREAM fallback proxy target. The ALB routes /v1/* to the API itself, so this is never used in AWS; it deliberately does not reference the ALB, whose DNS name changes when it is recreated."
  type        = string
  default     = "http://127.0.0.1:8000"
}

variable "alb_listener_arns" {
  description = "Listener ARNs. Services wait for them so target groups are attached to a load balancer first."
  type        = list(string)
  default     = []
}

variable "api_health_check_grace_period_seconds" {
  description = "Seconds ECS ignores failing load balancer health checks of a new API task. Must cover task start plus a database resume (about 15 s, 30 s or more after a day paused)."
  type        = number
  default     = 90
}

variable "web_health_check_grace_period_seconds" {
  description = "Seconds ECS ignores failing load balancer health checks of a new web task."
  type        = number
  default     = 60
}

variable "api_port" {
  description = "Container port of the API."
  type        = number
  default     = 8000
}

variable "web_port" {
  description = "Container port of the web tier."
  type        = number
  default     = 80
}

# --- data tier -------------------------------------------------------------------------------

variable "queue_url" {
  description = "SQS queue URL."
  type        = string
}

variable "queue_arn" {
  description = "SQS queue ARN."
  type        = string
}

variable "queue_name" {
  description = "SQS queue name (CloudWatch dimension for worker scaling)."
  type        = string
}

variable "bucket_name" {
  description = "Payload bucket name."
  type        = string
}

variable "bucket_arn" {
  description = "Payload bucket ARN."
  type        = string
}

variable "db_host" {
  description = "Database hostname."
  type        = string
}

variable "db_port" {
  description = "Database port."
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Database name."
  type        = string
}

variable "db_master_secret_arn" {
  description = "RDS-managed master secret; its JSON keys username and password are injected."
  type        = string
}

variable "api_key_pepper_secret_arn" {
  description = "Secret holding the API key pepper."
  type        = string
}

variable "model_api_key_secret_arn" {
  description = "Secret holding the model API key."
  type        = string
}

variable "model_base_url" {
  description = "Base URL of the OpenAI-compatible model endpoint (plain environment variable). Null leaves it unset."
  type        = string
  default     = null
}

# --- sizing ----------------------------------------------------------------------------------

variable "image_tag" {
  description = "Image tag the task definitions are created with. The deploy workflow replaces it with the commit SHA; Terraform ignores later service task-definition changes."
  type        = string
  default     = "bootstrap"
}

variable "adot_image" {
  description = "AWS Distro for OpenTelemetry collector image used as the sidecar."
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector:v0.49.0"
}

variable "api_desired_count" {
  description = "Number of API tasks."
  type        = number
  default     = 2
}

variable "web_desired_count" {
  description = "Number of web tasks."
  type        = number
  default     = 1
}

variable "worker_min" {
  description = "Minimum number of worker tasks."
  type        = number
  default     = 1
}

variable "worker_max" {
  description = "Maximum number of worker tasks."
  type        = number
  default     = 5
}

variable "worker_backlog_per_task" {
  description = "Target number of visible queue messages per running worker task."
  type        = number
  default     = 10
}

variable "api_cpu" {
  description = "Fargate CPU units of an API task."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Fargate memory (MiB) of an API task."
  type        = number
  default     = 1024
}

variable "worker_cpu" {
  description = "Fargate CPU units of a worker task."
  type        = number
  default     = 256
}

variable "worker_memory" {
  description = "Fargate memory (MiB) of a worker task."
  type        = number
  default     = 1024
}

variable "web_cpu" {
  description = "Fargate CPU units of a web task."
  type        = number
  default     = 256
}

variable "web_memory" {
  description = "Fargate memory (MiB) of a web task."
  type        = number
  default     = 512
}

variable "migrate_cpu" {
  description = "Fargate CPU units of the migrate task."
  type        = number
  default     = 256
}

variable "migrate_memory" {
  description = "Fargate memory (MiB) of the migrate task."
  type        = number
  default     = 512
}

# --- operations ------------------------------------------------------------------------------

variable "enable_ecs_exec" {
  description = "Allow ECS Exec into running tasks (adds SSM permissions to the task roles)."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 30
}

variable "metrics_namespace" {
  description = "CloudWatch namespace for application metrics emitted through EMF."
  type        = string
  default     = "ForgeEval"
}

variable "ecr_force_delete" {
  description = "Delete ECR repositories even when they still hold images (teardown)."
  type        = bool
  default     = false
}
