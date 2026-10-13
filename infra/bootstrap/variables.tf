variable "region" {
  description = "AWS region of the state bucket and the application stack."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Name prefix of the application stack (infra/envs/prod). The deploy role resource ARNs are derived from it, so both must agree."
  type        = string
  default     = "forgeeval-prod"
}

variable "github_repository" {
  description = "GitHub repository (owner/name) allowed to assume the roles."
  type        = string
  default     = "hxyng/forgeeval"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the form owner/name."
  }
}

variable "github_environment" {
  description = "GitHub Actions environment whose jobs may assume the deploy role."
  type        = string
  default     = "production"
}

variable "state_bucket_name" {
  description = "Name of the Terraform state bucket. Null derives <name_prefix>-tfstate-<account id>."
  type        = string
  default     = null
}

variable "existing_oidc_provider_arn" {
  description = "ARN of the account's existing GitHub OIDC provider. An account can hold only one per URL; set this to reuse it instead of creating one."
  type        = string
  default     = null
}

variable "state_noncurrent_version_days" {
  description = "Days old state versions are kept (the bucket is versioned so a bad apply can be undone)."
  type        = number
  default     = 90
}
