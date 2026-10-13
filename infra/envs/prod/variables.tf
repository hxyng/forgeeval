variable "region" {
  description = "AWS region for the whole environment."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name. infra/bootstrap derives the deploy role resource ARNs from the same value."
  type        = string
  default     = "forgeeval-prod"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,18}[a-z0-9]$", var.name_prefix))
    error_message = "Use 4-20 lowercase letters, digits and hyphens (load balancer and target group names are limited to 32 characters)."
  }
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "enable_nat_gateway" {
  description = "false: tasks run in public subnets with public IPs (cheaper; security groups still admit only the ALB). true: tasks run in private subnets behind a NAT gateway."
  type        = bool
  default     = false
}

variable "acm_certificate_arn" {
  description = "ACM certificate for HTTPS. When set, the ALB serves HTTPS and HTTP redirects to it."
  type        = string
  default     = null
}

variable "alb_access_logs_bucket" {
  description = "Existing S3 bucket for ALB access logs (its policy must allow the regional ELB log-delivery principal). Null disables access logs."
  type        = string
  default     = null
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial database storage in GiB."
  type        = number
  default     = 20
}

variable "multi_az" {
  description = "Run the database with a synchronous standby in a second AZ."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the database from deletion. Set to false and apply before terraform destroy."
  type        = bool
  default     = true
}

variable "payload_retention_days" {
  description = "Days before trace payloads in S3 expire."
  type        = number
  default     = 365
}

variable "api_desired_count" {
  description = "Number of API tasks. 2 spreads them over both zones, so one failing task or zone does not stop the API and rolling deploys keep capacity. 1 saves about $14 a month and means a single point of failure: ECS replaces a failed task, but requests fail for the one to two minutes that takes, and a deploy briefly runs two tasks."
  type        = number
  default     = 2

  validation {
    condition     = var.api_desired_count >= 1
    error_message = "api_desired_count must be at least 1."
  }
}

variable "worker_min_count" {
  description = "Minimum number of worker tasks. 0 scales the worker to zero when the queue is empty and back up on queue backlog (step scaling on SQS metrics, see modules/compute/scaling.tf); the first job then waits for a task to start, about one to two minutes."
  type        = number
  default     = 1

  validation {
    condition     = var.worker_min_count >= 0 && var.worker_min_count <= var.worker_max
    error_message = "worker_min_count must be between 0 and worker_max."
  }
}

variable "worker_max" {
  description = "Maximum number of worker tasks the queue-backlog scaling may reach."
  type        = number
  default     = 5
}

variable "enable_ecs_exec" {
  description = "Allow ECS Exec into tasks (off by default)."
  type        = bool
  default     = false
}

variable "model_base_url" {
  description = "Base URL of the OpenAI-compatible model endpoint. Null leaves MODEL_BASE_URL unset."
  type        = string
  default     = null
}

variable "alarm_email" {
  description = "Email address subscribed to the alarm topic (the subscription must be confirmed from the inbox). Empty means none."
  type        = string
  default     = ""
}

variable "teardown_force_delete" {
  description = "Teardown switch: let destroy delete the payload bucket and ECR repositories even when they still hold data."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag the task definitions are created with. Leave the default: the deploy workflow registers task definitions with the commit SHA."
  type        = string
  default     = "bootstrap"
}

variable "active" {
  description = "Demo mode switch. true: the full environment. false: asleep - no load balancer, listeners, target groups, ECS services or their autoscaling or alarms; the VPC, cluster, task definitions, ECR, queues, bucket, secrets, IAM, logs and database persist. See infra/scripts/demo.sh."
  type        = bool
  default     = true
}

variable "database_engine" {
  description = "rds: PostgreSQL 16 on a db.t4g.micro RDS instance. aurora-serverless: Aurora PostgreSQL 16 Serverless v2 that scales to 0 ACU and pauses when idle (cheapest while asleep). Changing it replaces the database."
  type        = string
  default     = "rds"

  validation {
    condition     = contains(["rds", "aurora-serverless"], var.database_engine)
    error_message = "database_engine must be \"rds\" or \"aurora-serverless\"."
  }
}

variable "aurora_max_acu" {
  description = "Aurora Serverless v2 maximum capacity in ACUs (aurora-serverless only)."
  type        = number
  default     = 2
}

variable "aurora_auto_pause_seconds" {
  description = "Idle seconds before Aurora pauses to 0 ACU, 300 to 86400 (aurora-serverless only)."
  type        = number
  default     = 300
}

# --- lean always-on options (see infra/README.md, "Lean always-on") --------------------------

variable "cpu_architecture" {
  description = "X86_64 or ARM64 (Graviton, about 20% cheaper) for every task definition. The deploy workflow reads it from the task definition and builds images for it. With ARM64 in us-east-1 the VPC skips zone use1-az3, where Fargate has no ARM64 capacity."
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be \"X86_64\" or \"ARM64\"."
  }
}

variable "worker_capacity" {
  description = "FARGATE or FARGATE_SPOT for the worker service only (the API and web stay on on-demand Fargate). Spot is discounted but can be interrupted with a two-minute warning, and Fargate never falls back to on-demand when Spot capacity is short."
  type        = string
  default     = "FARGATE"

  validation {
    condition     = contains(["FARGATE", "FARGATE_SPOT"], var.worker_capacity)
    error_message = "worker_capacity must be \"FARGATE\" or \"FARGATE_SPOT\"."
  }
}

variable "console_hosting" {
  description = "ecs: the console is an nginx task behind the load balancer. s3_cloudfront: static files in a private S3 bucket behind CloudFront (no web service or target group); CloudFront also routes /v1/*, /docs and /openapi.json to the load balancer."
  type        = string
  default     = "ecs"

  validation {
    condition     = contains(["ecs", "s3_cloudfront"], var.console_hosting)
    error_message = "console_hosting must be \"ecs\" or \"s3_cloudfront\"."
  }

  validation {
    condition     = var.console_hosting != "s3_cloudfront" || var.acm_certificate_arn == null
    error_message = "console_hosting = \"s3_cloudfront\" reaches the load balancer over HTTP at its own DNS name, which cannot carry a certificate: leave acm_certificate_arn null (CloudFront terminates HTTPS)."
  }
}

variable "container_insights" {
  description = "Container Insights on the ECS cluster. false drops its per-task metrics (cost); X-Ray traces and the application's EMF metrics stay. Worker scaling then uses step scaling on SQS metrics."
  type        = bool
  default     = true
}
