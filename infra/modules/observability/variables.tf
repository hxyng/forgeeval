variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
}

variable "alarm_email" {
  description = "Email address subscribed to the alarm topic. Empty means no subscription (the topic still exists)."
  type        = string
  default     = ""
}

variable "queue_name" {
  description = "Name of the eval-jobs queue."
  type        = string
}

variable "dlq_name" {
  description = "Name of the dead-letter queue."
  type        = string
}

variable "alb_arn_suffix" {
  description = "ARN suffix of the load balancer."
  type        = string
}

variable "api_target_group_arn_suffix" {
  description = "ARN suffix of the API target group."
  type        = string
}

variable "db_identifier" {
  description = "RDS instance identifier."
  type        = string
}

variable "queue_age_threshold_seconds" {
  description = "Alarm when the oldest job in the queue is older than this."
  type        = number
  default     = 600
}

variable "alb_5xx_rate_threshold_percent" {
  description = "Alarm when more than this percentage of requests return 5xx."
  type        = number
  default     = 5
}

variable "db_cpu_threshold_percent" {
  description = "RDS CPU alarm threshold."
  type        = number
  default     = 80
}

variable "db_free_storage_threshold_bytes" {
  description = "Alarm when RDS free storage falls below this many bytes (default 2 GiB)."
  type        = number
  default     = 2147483648
}
