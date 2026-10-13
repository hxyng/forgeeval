mock_provider "aws" {}

override_data {
  target = data.aws_availability_zones.available
  values = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
}

variables {
  name_prefix = "t"
}

run "ingress_is_internet_open_only_on_the_alb" {
  assert {
    condition = alltrue(concat(
      [for r in aws_vpc_security_group_ingress_rule.tasks_from_alb : r.cidr_ipv4 == null && r.cidr_ipv6 == null && r.referenced_security_group_id != null],
      [aws_vpc_security_group_ingress_rule.rds_from_tasks.cidr_ipv4 == null, aws_vpc_security_group_ingress_rule.rds_from_tasks.cidr_ipv6 == null],
    ))
    error_message = "Tasks and RDS must only accept traffic from other security groups."
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_http.cidr_ipv4 == "0.0.0.0/0" && aws_vpc_security_group_ingress_rule.alb_https.cidr_ipv4 == "0.0.0.0/0"
    error_message = "The ALB admits HTTP and HTTPS from the internet."
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_http.from_port == 80 && aws_vpc_security_group_ingress_rule.alb_https.from_port == 443
    error_message = "The ALB listens on 80 and 443 only."
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.rds_from_tasks.from_port == 5432 && aws_vpc_security_group_ingress_rule.rds_from_tasks.to_port == 5432
    error_message = "RDS admits only 5432."
  }

  assert {
    condition     = alltrue([for r in aws_vpc_security_group_egress_rule.alb_to_tasks : r.cidr_ipv4 == null])
    error_message = "The ALB forwards only to the task security group."
  }
}

run "tasks_are_public_without_nat" {
  assert {
    condition     = length(aws_nat_gateway.this) == 0 && output.assign_public_ip == true
    error_message = "No NAT gateway by default, so tasks need public IPs."
  }

  assert {
    condition     = output.task_subnet_ids == output.public_subnet_ids
    error_message = "Without NAT, tasks must run in the public subnets."
  }

  assert {
    condition     = length(output.public_subnet_ids) == 2 && length(output.private_subnet_ids) == 2
    error_message = "Two AZs, one public and one private subnet each."
  }

  assert {
    condition     = alltrue([for s in aws_subnet.public : s.map_public_ip_on_launch == false])
    error_message = "Subnets must not auto-assign public IPs."
  }
}

run "tasks_move_to_private_with_nat" {
  variables {
    enable_nat_gateway = true
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 1 && output.assign_public_ip == false
    error_message = "NAT enabled: one gateway, no public IPs on tasks."
  }

  assert {
    condition     = output.task_subnet_ids == output.private_subnet_ids && output.task_subnet_ids != output.public_subnet_ids
    error_message = "With NAT, tasks must run in the private subnets."
  }
}

run "excluded_zones_are_passed_to_the_zone_lookup" {
  variables {
    exclude_zone_ids = ["use1-az3"]
  }

  assert {
    condition     = output.excluded_zone_ids == toset(["use1-az3"])
    error_message = "Excluded zone IDs reach the availability zone lookup."
  }
}

run "no_zone_is_excluded_by_default" {
  assert {
    condition     = length(output.excluded_zone_ids) == 0
    error_message = "Default VPC layout is unchanged."
  }
}
