output "alb_dns_name" {
  description = "Public DNS name of the load balancer (the application URL until a domain is attached)."
  value       = module.alb.dns_name
}

output "ecr_repository_urls" {
  description = "ECR repository URLs by image (backend, web)."
  value       = module.compute.ecr_repository_urls
}

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = module.compute.cluster_name
}

output "api_service_name" {
  description = "API service name."
  value       = module.compute.api_service_name
}

output "worker_service_name" {
  description = "Worker service name."
  value       = module.compute.worker_service_name
}

output "web_service_name" {
  description = "Web service name."
  value       = module.compute.web_service_name
}

output "migrate_task_definition_family" {
  description = "Task definition family of the one-off migration task."
  value       = module.compute.migrate_task_definition_family
}

output "task_subnet_ids" {
  description = "Subnets for aws ecs run-task."
  value       = module.network.task_subnet_ids
}

output "task_security_group_id" {
  description = "Security group for aws ecs run-task."
  value       = module.network.tasks_security_group_id
}

output "assign_public_ip" {
  description = "Value for run-task assignPublicIp (ENABLED when true, DISABLED when false)."
  value       = module.network.assign_public_ip
}

output "model_api_key_secret_name" {
  description = "Secret to fill with aws secretsmanager put-secret-value."
  value       = module.data.model_api_key_secret_name
}

output "db_master_secret_arn" {
  description = "RDS-managed master credentials secret."
  value       = module.data.db_master_secret_arn
}

output "alarm_topic_arn" {
  description = "SNS topic receiving alarms."
  value       = module.observability.alarm_topic_arn
}
