variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
}

variable "vpc_id" {
  description = "VPC the target groups live in."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnets the load balancer spans."
  type        = list(string)
}

variable "security_group_id" {
  description = "Security group of the load balancer."
  type        = string
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

variable "acm_certificate_arn" {
  description = "ACM certificate for the HTTPS listener. When set, HTTP redirects to HTTPS; when null only HTTP is served."
  type        = string
  default     = null
}

variable "ssl_policy" {
  description = "TLS policy of the HTTPS listener."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-Res-2021-06"
}

variable "access_logs_bucket" {
  description = "Existing S3 bucket that receives ALB access logs. Null disables access logs."
  type        = string
  default     = null
}

variable "deletion_protection" {
  description = "Protect the load balancer from deletion."
  type        = bool
  default     = false
}

variable "web_enabled" {
  description = "Create the web target group and forward unmatched paths to it. False when the console is served from S3 and CloudFront: unmatched paths then get a 404."
  type        = bool
  default     = true
}
