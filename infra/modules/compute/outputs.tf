output "cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.this.arn
}

output "api_service_name" {
  description = "Name of the API service."
  value       = aws_ecs_service.api.name
}

output "worker_service_name" {
  description = "Name of the worker service."
  value       = aws_ecs_service.worker.name
}

output "web_service_name" {
  description = "Name of the web service."
  value       = aws_ecs_service.web.name
}

output "migrate_task_definition_family" {
  description = "Family of the one-off migrate task definition (run before each deploy)."
  value       = aws_ecs_task_definition.this["migrate"].family
}

output "ecr_repository_urls" {
  description = "ECR repository URLs by image (backend, web)."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "service_subnet_ids" {
  description = "Subnets the services were configured with."
  value       = aws_ecs_service.api.network_configuration[0].subnets
}

output "service_assign_public_ip" {
  description = "Whether the services assign public IPs."
  value       = aws_ecs_service.api.network_configuration[0].assign_public_ip
}

output "execution_role_arn" {
  description = "Task execution role ARN."
  value       = aws_iam_role.execution.arn
}

output "task_role_arns" {
  description = "Task role ARNs by role."
  value = {
    api    = aws_iam_role.api_task.arn
    worker = aws_iam_role.worker_task.arn
  }
}

output "log_group_names" {
  description = "CloudWatch log groups by service."
  value       = { for k, g in aws_cloudwatch_log_group.service : k => g.name }
}
