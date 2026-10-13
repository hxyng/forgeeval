data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs           = slice(data.aws_availability_zones.available.names, 0, 2)
  az_index      = { for i, az in local.azs : az => i }
  nat_enabled   = var.enable_nat_gateway
  first_public  = local.azs[0]
  task_subnets  = local.nat_enabled ? aws_subnet.private : aws_subnet.public
  task_ports    = { api = var.api_port, web = var.web_port }
  https_egress  = 443
  postgres_port = 5432
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name_prefix }
}

# The default security group is emptied so nothing can use it by accident.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-default-unused" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = var.name_prefix }
}

resource "aws_subnet" "public" {
  for_each = local.az_index

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value)
  # Public IPs are requested per ECS task (assign_public_ip), never by default on the subnet.
  map_public_ip_on_launch = false

  tags = { Name = "${var.name_prefix}-public-${each.key}", Tier = "public" }
}

resource "aws_subnet" "private" {
  for_each = local.az_index

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value + 10)

  tags = { Name = "${var.name_prefix}-private-${each.key}", Tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-private" }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# One NAT gateway (single AZ) keeps the optional private mode affordable; it is a single point of failure by design.
resource "aws_eip" "nat" {
  count = local.nat_enabled ? 1 : 0

  domain = "vpc"

  tags = { Name = "${var.name_prefix}-nat" }
}

resource "aws_nat_gateway" "this" {
  count = local.nat_enabled ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[local.first_public].id

  tags = { Name = var.name_prefix }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route" "private_nat" {
  count = local.nat_enabled ? 1 : 0

  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

# ---------------------------------------------------------------------------------------------
# Security groups: ALB (internet) -> tasks (api/web/worker) -> RDS
# ---------------------------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public application load balancer"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-alb" }

  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "tasks" {
  name        = "${var.name_prefix}-tasks"
  description = "ECS tasks: reachable only from the load balancer"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-tasks" }

  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds"
  description = "PostgreSQL: reachable only from ECS tasks"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-rds" }

  lifecycle { create_before_destroy = true }
}

# The load balancer is the only thing in this stack that accepts traffic from the internet.
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from the internet"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  for_each = local.task_ports

  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to ${each.key} tasks on ${each.value}"
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  referenced_security_group_id = aws_security_group.tasks.id
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  for_each = local.task_ports

  security_group_id            = aws_security_group.tasks.id
  description                  = "From the load balancer to ${each.key} on ${each.value}"
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  referenced_security_group_id = aws_security_group.alb.id
}

# Tasks call ECR, S3, SQS, Secrets Manager, CloudWatch, X-Ray and the model API, all over HTTPS.
resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  description       = "HTTPS to AWS APIs and the model endpoint"
  ip_protocol       = "tcp"
  from_port         = local.https_egress
  to_port           = local.https_egress
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_rds" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = local.postgres_port
  to_port                      = local.postgres_port
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_tasks" {
  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL from ECS tasks"
  ip_protocol                  = "tcp"
  from_port                    = local.postgres_port
  to_port                      = local.postgres_port
  referenced_security_group_id = aws_security_group.tasks.id
}
