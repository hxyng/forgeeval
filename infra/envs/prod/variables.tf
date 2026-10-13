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
  description = "Number of API tasks."
  type        = number
  default     = 2
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
