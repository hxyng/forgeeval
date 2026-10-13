variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
}

variable "alb_dns_name" {
  description = "DNS name of the application load balancer that serves /v1/*, /docs and /openapi.json. Null while the environment sleeps (demo mode): the origin then points at a name that does not resolve, so the API answers 502 and the console still loads."
  type        = string
  default     = null
}

variable "api_path_patterns" {
  description = "Paths CloudFront forwards to the load balancer; everything else is the console."
  type        = list(string)
  default     = ["/v1/*", "/docs", "/openapi.json"]
}

variable "price_class" {
  description = "CloudFront price class. PriceClass_100 serves North America and Europe only and is the cheapest."
  type        = string
  default     = "PriceClass_100"
}

variable "force_destroy" {
  description = "Let destroy delete the console bucket even when it holds files (teardown)."
  type        = bool
  default     = false
}
