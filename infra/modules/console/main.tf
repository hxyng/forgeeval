# Console hosting on S3 + CloudFront. The browser talks to one origin (the distribution): static
# files come from a private bucket through Origin Access Control, and /v1/*, /docs and /openapi.json
# go to the load balancer, so the console stays same-origin and the API needs no CORS.

data "aws_caller_identity" "current" {}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# Forwards every viewer header (Authorization, X-API-Key, ...), query string and cookie to the API,
# except Host (CloudFront sets the origin's own Host).
data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

locals {
  bucket_name   = "${var.name_prefix}-console-${data.aws_caller_identity.current.account_id}"
  s3_origin_id  = "console"
  alb_origin_id = "api"
  # While asleep there is no load balancer. ".invalid" never resolves (RFC 2606), so the API paths
  # fail fast with a 502 instead of reaching someone else's host.
  alb_origin_domain = coalesce(var.alb_dns_name, "asleep.invalid")
}

# ---------------------------------------------------------------------------------------------
# Bucket: private, reachable only by this distribution
# ---------------------------------------------------------------------------------------------

resource "aws_s3_bucket" "console" {
  bucket        = local.bucket_name
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "console" {
  bucket = aws_s3_bucket.console.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "console" {
  bucket = aws_s3_bucket.console.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "console" {
  bucket = aws_s3_bucket.console.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "console" {
  bucket = aws_s3_bucket.console.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontRead"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.console.arn}/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.this.arn } }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.console.arn, "${aws_s3_bucket.console.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.console]
}

# ---------------------------------------------------------------------------------------------
# CloudFront
# ---------------------------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "console" {
  name                              = "${var.name_prefix}-console"
  description                       = "Signed requests from CloudFront to the console bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Client-side routes (/runs/123) are not files. This rewrites them to /index.html on the console
# behaviour only. A distribution-wide custom error response (403/404 to index.html) would also
# rewrite the API's own 403 and 404 answers into a 200 HTML page, so it is deliberately not used.
resource "aws_cloudfront_function" "spa_fallback" {
  name    = "${var.name_prefix}-spa-fallback"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrite extensionless paths to /index.html (console behaviour only)"
  publish = true

  code = <<-JS
    function handler(event) {
      var request = event.request;
      if (!/\.[A-Za-z0-9]+$/.test(request.uri)) {
        request.uri = "/index.html";
      }
      return request;
    }
  JS
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${var.name_prefix} console and API"
  default_root_object = "index.html"
  price_class         = var.price_class
  http_version        = "http2and3"

  origin {
    origin_id                = local.s3_origin_id
    domain_name              = aws_s3_bucket.console.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.console.id
  }

  origin {
    origin_id   = local.alb_origin_id
    domain_name = local.alb_origin_domain

    # The load balancer has no certificate for its own DNS name, so CloudFront reaches it over HTTP.
    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id       = local.s3_origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_fallback.arn
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = var.api_path_patterns

    content {
      path_pattern             = ordered_cache_behavior.value
      target_origin_id         = local.alb_origin_id
      viewer_protocol_policy   = "redirect-to-https"
      allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods           = ["GET", "HEAD"]
      compress                 = true
      cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
