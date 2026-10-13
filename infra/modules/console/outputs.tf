output "bucket_name" {
  description = "Private bucket the deploy workflow syncs the built console into."
  value       = aws_s3_bucket.console.bucket
}

output "bucket_arn" {
  description = "ARN of the console bucket."
  value       = aws_s3_bucket.console.arn
}

output "distribution_id" {
  description = "CloudFront distribution ID (for invalidations)."
  value       = aws_cloudfront_distribution.this.id
}

output "distribution_arn" {
  description = "CloudFront distribution ARN."
  value       = aws_cloudfront_distribution.this.arn
}

output "domain_name" {
  description = "CloudFront domain name: the application URL (HTTPS)."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "api_origin_domain" {
  description = "Domain name of the API origin (the load balancer, or a non-resolving name while asleep)."
  value       = local.alb_origin_domain
}
