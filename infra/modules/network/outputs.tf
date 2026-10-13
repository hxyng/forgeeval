output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "Public subnet ids (one per AZ); the load balancer lives here."
  value       = [for az in local.azs : aws_subnet.public[az].id]
}

output "private_subnet_ids" {
  description = "Private subnet ids (one per AZ); RDS lives here, and tasks too when the NAT gateway is enabled."
  value       = [for az in local.azs : aws_subnet.private[az].id]
}

output "task_subnet_ids" {
  description = "Subnets ECS tasks run in: public when the NAT gateway is disabled, private when enabled."
  value       = [for az in local.azs : local.task_subnets[az].id]
}

output "assign_public_ip" {
  description = "Whether ECS tasks need a public IP (true when there is no NAT gateway)."
  value       = !local.nat_enabled
}

output "alb_security_group_id" {
  description = "Security group of the load balancer."
  value       = aws_security_group.alb.id
}

output "tasks_security_group_id" {
  description = "Security group shared by all ECS tasks."
  value       = aws_security_group.tasks.id
}

output "rds_security_group_id" {
  description = "Security group of the database."
  value       = aws_security_group.rds.id
}

output "excluded_zone_ids" {
  description = "Availability zone IDs the VPC avoids."
  value       = data.aws_availability_zones.available.exclude_zone_ids
}
