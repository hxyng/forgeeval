variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets for the RDS subnet group."
  type        = list(string)
}

variable "rds_security_group_id" {
  description = "Security group attached to the database."
  type        = string
}

variable "db_name" {
  description = "Name of the initial database."
  type        = string
  default     = "forgeeval"
}

variable "db_username" {
  description = "Master username. The password is generated and held by RDS in Secrets Manager."
  type        = string
  default     = "forgeeval"
}

variable "db_engine_version" {
  description = "PostgreSQL engine version. A bare major lets RDS pick (and auto-upgrade) the minor."
  type        = string
  default     = "16"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial storage in GiB (gp3)."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB."
  type        = number
  default     = 100
}

variable "multi_az" {
  description = "Run a synchronous standby in a second AZ."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Block deletion of the database. Turn it off (and apply) before terraform destroy."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot on destroy. Keep false in production."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention."
  type        = number
  default     = 7
}

variable "queue_visibility_timeout_seconds" {
  description = "Visibility timeout of the job queue; must match the worker lease (docs/adr/0001-job-queue.md)."
  type        = number
  default     = 120
}

variable "queue_max_receive_count" {
  description = "Receives before a message is moved to the dead-letter queue."
  type        = number
  default     = 5
}

variable "payload_retention_days" {
  description = "Days before trace payload objects expire."
  type        = number
  default     = 365

  validation {
    condition     = var.payload_retention_days > 30
    error_message = "Retention must exceed the 30 days after which objects move to STANDARD_IA."
  }
}

variable "payload_force_destroy" {
  description = "Allow destroying the payload bucket while it still holds objects (teardown only)."
  type        = bool
  default     = false
}

variable "secret_recovery_window_days" {
  description = "Recovery window when a secret is deleted (0 deletes immediately)."
  type        = number
  default     = 7
}

variable "database_engine" {
  description = "rds: PostgreSQL 16 on a single RDS instance. aurora-serverless: Aurora PostgreSQL 16 Serverless v2 that scales to 0 ACU and pauses when idle (demo mode)."
  type        = string
  default     = "rds"

  validation {
    condition     = contains(["rds", "aurora-serverless"], var.database_engine)
    error_message = "database_engine must be \"rds\" or \"aurora-serverless\"."
  }
}

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL engine version. Scaling to 0 ACU needs 16.3 or newer."
  type        = string
  default     = "16.6"

  validation {
    condition     = can(regex("^16[.]([3-9]|[1-9][0-9])$", var.aurora_engine_version))
    error_message = "Use Aurora PostgreSQL 16.3 or newer (earlier 16.x minors cannot scale to 0 ACU)."
  }
}

variable "aurora_max_acu" {
  description = "Aurora Serverless v2 maximum capacity in ACUs (1 ACU is about 2 GiB of memory)."
  type        = number
  default     = 2

  validation {
    condition     = var.aurora_max_acu >= 1 && var.aurora_max_acu <= 256
    error_message = "Aurora needs a maximum of at least 1 ACU when the minimum is 0."
  }
}

variable "aurora_auto_pause_seconds" {
  description = "Idle time (no connections) before Aurora pauses to 0 ACU. AWS allows 300 to 86400."
  type        = number
  default     = 300

  validation {
    condition     = var.aurora_auto_pause_seconds >= 300 && var.aurora_auto_pause_seconds <= 86400
    error_message = "seconds_until_auto_pause must be between 300 and 86400."
  }
}
