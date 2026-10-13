mock_provider "aws" {
  mock_resource "aws_s3_bucket" {
    defaults = {
      arn                         = "arn:aws:s3:::t-console-123456789012"
      bucket_regional_domain_name = "t-console-123456789012.s3.us-east-1.amazonaws.com"
    }
  }

  mock_resource "aws_cloudfront_distribution" {
    defaults = {
      arn         = "arn:aws:cloudfront::123456789012:distribution/E1ABCDEF"
      domain_name = "d111111abcdef8.cloudfront.net"
    }
  }

  mock_resource "aws_cloudfront_function" {
    defaults = { arn = "arn:aws:cloudfront::123456789012:function/t-spa-fallback" }
  }
}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}

variables {
  name_prefix  = "t"
  alb_dns_name = "t-alb-1.us-east-1.elb.amazonaws.com"
}

run "bucket_is_private_encrypted_and_named_after_the_account" {
  assert {
    condition     = aws_s3_bucket.console.bucket == "t-console-123456789012" && output.bucket_name == "t-console-123456789012"
    error_message = "The bucket name is <prefix>-console-<account id> (the deploy role is scoped to it)."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.console.block_public_acls,
      aws_s3_bucket_public_access_block.console.block_public_policy,
      aws_s3_bucket_public_access_block.console.ignore_public_acls,
      aws_s3_bucket_public_access_block.console.restrict_public_buckets,
    ])
    error_message = "All four public access blocks must be on."
  }

  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.console.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256" && aws_s3_bucket.console.force_destroy == false
    error_message = "Encrypted, and not force-destroyable by default."
  }
}

run "bucket_policy_admits_only_this_distribution_over_tls" {
  assert {
    condition = (
      length([for s in jsondecode(aws_s3_bucket_policy.console.policy).Statement : s if s.Effect == "Allow"]) == 1 &&
      one([for s in jsondecode(aws_s3_bucket_policy.console.policy).Statement : s.Principal.Service if s.Effect == "Allow"]) == "cloudfront.amazonaws.com" &&
      one([for s in jsondecode(aws_s3_bucket_policy.console.policy).Statement : s.Action if s.Effect == "Allow"]) == "s3:GetObject" &&
      one([for s in jsondecode(aws_s3_bucket_policy.console.policy).Statement : s.Resource if s.Effect == "Allow"]) == "arn:aws:s3:::t-console-123456789012/*"
    )
    error_message = "The only Allow is s3:GetObject for the CloudFront service principal on the bucket's objects."
  }

  assert {
    condition     = one([for s in jsondecode(aws_s3_bucket_policy.console.policy).Statement : s.Condition.StringEquals["AWS:SourceArn"] if s.Effect == "Allow"]) == aws_cloudfront_distribution.this.arn
    error_message = "The Allow is restricted to this distribution through AWS:SourceArn."
  }

  assert {
    condition     = one([for s in jsondecode(aws_s3_bucket_policy.console.policy).Statement : s.Condition.Bool["aws:SecureTransport"] if s.Effect == "Deny"]) == "false"
    error_message = "Plain HTTP access is denied."
  }
}

run "origin_access_control_signs_s3_requests" {
  assert {
    condition     = aws_cloudfront_origin_access_control.console.origin_access_control_origin_type == "s3" && aws_cloudfront_origin_access_control.console.signing_behavior == "always" && aws_cloudfront_origin_access_control.console.signing_protocol == "sigv4"
    error_message = "OAC with always-on SigV4."
  }

  assert {
    condition     = one([for o in aws_cloudfront_distribution.this.origin : o.origin_access_control_id if o.origin_id == "console"]) == aws_cloudfront_origin_access_control.console.id
    error_message = "The S3 origin uses the OAC (no origin access identity, no public bucket)."
  }
}

run "api_paths_go_to_the_load_balancer_and_the_rest_to_s3" {
  assert {
    condition     = toset([for b in aws_cloudfront_distribution.this.ordered_cache_behavior : b.path_pattern]) == toset(["/v1/*", "/docs", "/openapi.json"])
    error_message = "/v1/*, /docs and /openapi.json have their own behaviours."
  }

  assert {
    condition     = alltrue([for b in aws_cloudfront_distribution.this.ordered_cache_behavior : b.target_origin_id == "api" && contains(b.allowed_methods, "POST") && contains(b.allowed_methods, "DELETE")])
    error_message = "API behaviours target the ALB origin and allow every method."
  }

  assert {
    condition     = one([for o in aws_cloudfront_distribution.this.origin : o.domain_name if o.origin_id == "api"]) == "t-alb-1.us-east-1.elb.amazonaws.com" && one([for o in aws_cloudfront_distribution.this.origin : one(o.custom_origin_config).origin_protocol_policy if o.origin_id == "api"]) == "http-only"
    error_message = "The ALB origin is the load balancer DNS name over HTTP."
  }

  assert {
    condition     = one(aws_cloudfront_distribution.this.default_cache_behavior).target_origin_id == "console" && toset(one(aws_cloudfront_distribution.this.default_cache_behavior).allowed_methods) == toset(["GET", "HEAD"])
    error_message = "Everything else is the read-only console bucket."
  }

  assert {
    condition     = alltrue([for b in concat([one(aws_cloudfront_distribution.this.default_cache_behavior)], tolist(aws_cloudfront_distribution.this.ordered_cache_behavior)) : b.viewer_protocol_policy == "redirect-to-https"])
    error_message = "Viewers are redirected to HTTPS on every behaviour."
  }
}

run "spa_fallback_is_a_viewer_request_function_not_a_custom_error_response" {
  assert {
    condition     = one(one(aws_cloudfront_distribution.this.default_cache_behavior).function_association).event_type == "viewer-request" && one(one(aws_cloudfront_distribution.this.default_cache_behavior).function_association).function_arn == aws_cloudfront_function.spa_fallback.arn
    error_message = "Client-side routes are rewritten to /index.html on the console behaviour."
  }

  assert {
    condition     = strcontains(aws_cloudfront_function.spa_fallback.code, "/index.html") && aws_cloudfront_function.spa_fallback.runtime == "cloudfront-js-2.0"
    error_message = "The function rewrites to /index.html."
  }

  assert {
    condition     = length(aws_cloudfront_distribution.this.custom_error_response) == 0
    error_message = "A distribution-wide custom error response would turn the API's own 403 and 404 into 200 HTML."
  }

  assert {
    condition     = length([for b in aws_cloudfront_distribution.this.ordered_cache_behavior : b if length(b.function_association) > 0]) == 0
    error_message = "The rewrite must not touch API paths."
  }

  assert {
    condition     = aws_cloudfront_distribution.this.default_root_object == "index.html"
    error_message = "The root serves index.html."
  }
}

run "asleep_points_the_api_origin_at_a_name_that_never_resolves" {
  variables {
    alb_dns_name = null
  }

  assert {
    condition     = one([for o in aws_cloudfront_distribution.this.origin : o.domain_name if o.origin_id == "api"]) == "asleep.invalid"
    error_message = "With no load balancer the API origin is a reserved, non-resolving name."
  }

  assert {
    condition     = one(aws_cloudfront_distribution.this.default_cache_behavior).target_origin_id == "console" && aws_s3_bucket.console.bucket == "t-console-123456789012"
    error_message = "The console and the bucket are unaffected."
  }
}
