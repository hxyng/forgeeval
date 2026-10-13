output "cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.this.arn
}

output "api_service_name" {
  description = "Name of the API service (null while asleep)."
  value       = one(aws_ecs_service.api[*].name)
}

output "worker_service_name" {
  description = "Name of the worker service (null while asleep)."
  value       = one(aws_ecs_service.worker[*].name)
}

output "web_service_name" {
  description = "Name of the web service (null while asleep)."
  value       = one(aws_ecs_service.web[*].name)
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
  value       = one(aws_ecs_service.api[*].network_configuration[0].subnets)
}

output "service_assign_public_ip" {
  description = "Whether the services assign public IPs."
  value       = one(aws_ecs_service.api[*].network_configuration[0].assign_public_ip)
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

output "task_definition_fingerprints" {
  description = "SHA-256 of each task definition's content (container definitions, size, roles). Identical awake and asleep: demo mode never touches them."
  value = {
    for k, t in aws_ecs_task_definition.this :
    k => sha256(join("|", [t.container_definitions, t.cpu, t.memory, t.execution_role_arn, coalesce(t.task_role_arn, "none")]))
  }
}

output "service_latest_task_definition_arns" {
  description = "Task definition each service is created from: the latest ACTIVE revision of its family (empty while asleep)."
  value       = { for k, d in data.aws_ecs_task_definition.latest : k => d.arn }
}

output "service_health_check_grace_periods" {
  description = "Load balancer health check grace period of the api and web services (empty while asleep)."
  value = merge(
    { for s in aws_ecs_service.api : "api" => s.health_check_grace_period_seconds },
    { for s in aws_ecs_service.web : "web" => s.health_check_grace_period_seconds },
  )
}

output "task_runtime_platforms" {
  description = "CPU architecture of each task definition."
  value       = { for k, t in aws_ecs_task_definition.this : k => one(t.runtime_platform).cpu_architecture }
}

output "service_capacity_providers" {
  description = "Capacity providers each service is pinned to (empty list: plain Fargate launch type). Empty map while asleep."
  value = merge(
    { for s in aws_ecs_service.api : "api" => [for c in s.capacity_provider_strategy : c.capacity_provider] },
    { for s in aws_ecs_service.web : "web" => [for c in s.capacity_provider_strategy : c.capacity_provider] },
    { for s in aws_ecs_service.worker : "worker" => [for c in s.capacity_provider_strategy : c.capacity_provider] },
  )
}

output "service_desired_counts" {
  description = "Initial desired count of each service (the worker count is then owned by autoscaling). Empty map while asleep."
  value = merge(
    { for s in aws_ecs_service.api : "api" => s.desired_count },
    { for s in aws_ecs_service.web : "web" => s.desired_count },
    { for s in aws_ecs_service.worker : "worker" => s.desired_count },
  )
}

output "worker_scaling" {
  description = "Worker autoscaling: mode (target-tracking or step), minimum and maximum. Null while asleep."
  value = one([for t in aws_appautoscaling_target.worker : {
    mode = local.step_scaling ? "step" : "target-tracking"
    min  = t.min_capacity
    max  = t.max_capacity
  }])
}

output "container_insights" {
  description = "Container Insights setting of the cluster."
  value       = one(aws_ecs_cluster.this.setting).value
}
