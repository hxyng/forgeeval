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

variable "alb_enabled" {
  description = "Create the load balancer alarms. False while the environment sleeps (no load balancer exists)."
  type        = bool
  default     = true
}

variable "alb_arn_suffix" {
  description = "ARN suffix of the load balancer (null when alb_enabled is false)."
  type        = string
  default     = null
}

variable "api_target_group_arn_suffix" {
  description = "ARN suffix of the API target group (null when alb_enabled is false)."
  type        = string
  default     = null
}

variable "db_engine" {
  description = "rds or aurora-serverless: selects the storage/capacity alarm."
  type        = string
  default     = "rds"

  validation {
    condition     = contains(["rds", "aurora-serverless"], var.db_engine)
    error_message = "db_engine must be \"rds\" or \"aurora-serverless\"."
  }
}

variable "db_identifier" {
  description = "DB instance identifier (RDS instance or the Aurora cluster's instance)."
  type        = string
}

variable "db_cluster_identifier" {
  description = "Aurora cluster identifier; required when db_engine is aurora-serverless."
  type        = string
  default     = null
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

variable "aurora_acu_utilization_threshold_percent" {
  description = "Aurora ACUUtilization alarm threshold (percent of the maximum capacity)."
  type        = number
  default     = 90
}
