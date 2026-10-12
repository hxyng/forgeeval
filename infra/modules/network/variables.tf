variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC. Subnets are carved out of it as /24s."
  type        = string
  default     = "10.20.0.0/16"
}

variable "enable_nat_gateway" {
  description = "When false (cost default) ECS tasks run in public subnets with public IPs; when true they move to private subnets behind a single NAT gateway."
  type        = bool
  default     = false
}

variable "api_port" {
  description = "Container port of the API."
  type        = number
  default     = 8000
}

variable "web_port" {
  description = "Container port of the web (nginx) tier."
  type        = number
  default     = 80
}
