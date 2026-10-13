output "dns_name" {
  description = "DNS name of the load balancer."
  value       = aws_lb.this.dns_name
}

output "arn_suffix" {
  description = "ARN suffix of the load balancer (CloudWatch dimension)."
  value       = aws_lb.this.arn_suffix
}

output "name" {
  description = "Name of the load balancer."
  value       = aws_lb.this.name
}

output "api_target_group_arn" {
  description = "Target group of the API."
  value       = aws_lb_target_group.api.arn
}

output "api_target_group_arn_suffix" {
  description = "ARN suffix of the API target group (CloudWatch dimension)."
  value       = aws_lb_target_group.api.arn_suffix
}

output "web_target_group_arn" {
  description = "Target group of the web tier (null when the console is served from S3 and CloudFront)."
  value       = one(aws_lb_target_group.web[*].arn)
}

output "https_enabled" {
  description = "Whether an HTTPS listener exists."
  value       = local.https_enabled
}

output "listener_arns" {
  description = "Listeners the ECS services must wait for before registering targets."
  value       = compact([aws_lb_listener.http.arn, try(aws_lb_listener.https[0].arn, "")])
}
