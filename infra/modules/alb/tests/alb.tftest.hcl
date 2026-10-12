mock_provider "aws" {
  mock_resource "aws_lb" {
    defaults = {
      arn        = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/t-alb/50dc6c495c0c9188"
      arn_suffix = "app/t-alb/50dc6c495c0c9188"
      dns_name   = "t-alb-1.us-east-1.elb.amazonaws.com"
    }
  }
}

override_resource {
  target = aws_lb_target_group.api
  values = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/t-api/1111111111111111" }
}

override_resource {
  target = aws_lb_target_group.web
  values = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/t-web/2222222222222222" }
}

override_resource {
  target = aws_lb_listener.http
  values = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/t-alb/50dc6c495c0c9188/aaaaaaaaaaaaaaaa" }
}

variables {
  name_prefix       = "t"
  vpc_id            = "vpc-1"
  public_subnet_ids = ["subnet-a", "subnet-b"]
  security_group_id = "sg-alb"
}

run "http_only_without_a_certificate" {
  assert {
    condition     = length(aws_lb_listener.https) == 0 && output.https_enabled == false
    error_message = "No certificate, no HTTPS listener."
  }

  assert {
    condition     = one(aws_lb_listener.http.default_action).type == "forward"
    error_message = "HTTP forwards to the web tier when there is no certificate."
  }

  assert {
    condition     = aws_lb.this.internal == false && aws_lb.this.drop_invalid_header_fields == true
    error_message = "Public ALB that drops invalid headers."
  }

  assert {
    condition     = one(aws_lb_listener_rule.api.condition).path_pattern[0].values == toset(["/v1/*", "/docs", "/openapi.json", "/healthz", "/readyz"])
    error_message = "API paths route to the API target group."
  }

  assert {
    condition     = one(aws_lb_target_group.api.health_check).path == "/readyz" && one(aws_lb_target_group.web.health_check).path == "/healthz"
    error_message = "Health checks: /readyz for the API, /healthz for the web tier."
  }
}

run "https_redirects_http" {
  variables {
    acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/abc"
  }

  override_resource {
    target = aws_lb_listener.https[0]
    values = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/t-alb/50dc6c495c0c9188/bbbbbbbbbbbbbbbb" }
  }

  assert {
    condition     = aws_lb_listener_rule.api.listener_arn != aws_lb_listener.http.arn
    error_message = "Path rules must not attach to the redirecting HTTP listener."
  }

  assert {
    condition     = length(aws_lb_listener.https) == 1 && one(aws_lb_listener.http.default_action).type == "redirect"
    error_message = "With a certificate HTTP must redirect to HTTPS."
  }

  assert {
    condition     = aws_lb_listener_rule.api.listener_arn == aws_lb_listener.https[0].arn
    error_message = "Path rules attach to the HTTPS listener."
  }
}
