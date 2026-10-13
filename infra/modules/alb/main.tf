locals {
  https_enabled = var.acm_certificate_arn != null
  # Path rules live on the listener that serves the application (HTTPS when a certificate is set).
  app_listener_arn = local.https_enabled ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
  api_paths        = ["/v1/*", "/docs", "/openapi.json", "/healthz", "/readyz"]
}

# Internet-facing by design: this is the public entry point of the product.
resource "aws_lb" "this" {
  name                       = "${var.name_prefix}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [var.security_group_id]
  subnets                    = var.public_subnet_ids
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
  idle_timeout               = 60

  dynamic "access_logs" {
    for_each = var.access_logs_bucket == null ? [] : [var.access_logs_bucket]

    content {
      bucket  = access_logs.value
      prefix  = var.name_prefix
      enabled = true
    }
  }
}

resource "aws_lb_target_group" "api" {
  name                 = "${var.name_prefix}-api"
  port                 = var.api_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path                = "/readyz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  lifecycle { create_before_destroy = true }
}

# Without the web tier (console on S3 and CloudFront) there is no web target group.
moved {
  from = aws_lb_target_group.web
  to   = aws_lb_target_group.web[0]
}

resource "aws_lb_target_group" "web" {
  count = var.web_enabled ? 1 : 0

  name                 = "${var.name_prefix}-web"
  port                 = var.web_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  lifecycle { create_before_destroy = true }
}

# Plain HTTP when there is no certificate (e.g. before a domain exists); otherwise it only redirects.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = local.https_enabled ? [1] : []

    content {
      type = "redirect"

      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = local.https_enabled || !var.web_enabled ? [] : [1]

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.web[0].arn
    }
  }

  dynamic "default_action" {
    for_each = local.https_enabled || var.web_enabled ? [] : [1]

    content {
      type = "fixed-response"

      fixed_response {
        content_type = "text/plain"
        message_body = "not found"
        status_code  = "404"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = local.https_enabled ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.ssl_policy
  certificate_arn   = var.acm_certificate_arn

  dynamic "default_action" {
    for_each = var.web_enabled ? [1] : []

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.web[0].arn
    }
  }

  dynamic "default_action" {
    for_each = var.web_enabled ? [] : [1]

    content {
      type = "fixed-response"

      fixed_response {
        content_type = "text/plain"
        message_body = "not found"
        status_code  = "404"
      }
    }
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = local.app_listener_arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    path_pattern {
      values = local.api_paths
    }
  }
}
