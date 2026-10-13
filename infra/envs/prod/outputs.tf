output "alb_dns_name" {
  description = "Public DNS name of the load balancer (the application URL until a domain is attached). Null while the environment sleeps (active = false)."
  value       = one(module.alb[*].dns_name)
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

output "active" {
  description = "Whether the environment is awake (load balancer and services exist)."
  value       = var.active
}

output "database_engine" {
  description = "Database engine in use (rds or aurora-serverless)."
  value       = var.database_engine
}

output "persistent_state" {
  description = "Identifying attributes of everything that survives sleep (task definitions, cluster, database, queues, bucket, secrets, ECR, roles). Must be identical for active = true and false; the tests assert it."
  value = {
    task_definitions = module.compute.task_definition_fingerprints
    cluster_arn      = module.compute.cluster_arn
    database         = [module.data.db_engine, module.data.db_identifier, module.data.db_host, module.data.db_port, module.data.db_name, module.data.db_master_secret_arn]
    queues           = [module.data.queue_url, module.data.queue_arn, module.data.dlq_arn]
    bucket           = module.data.bucket_arn
    secrets          = [module.data.api_key_pepper_secret_arn, module.data.model_api_key_secret_arn]
    ecr              = module.compute.ecr_repository_urls
    roles            = [module.compute.execution_role_arn, module.compute.task_role_arns]
    console          = [one(module.console[*].bucket_arn), one(module.console[*].distribution_arn)]
  }
}

output "console_hosting" {
  description = "Where the console is served from (ecs or s3_cloudfront)."
  value       = var.console_hosting
}

output "cpu_architecture" {
  description = "CPU architecture of the task definitions."
  value       = var.cpu_architecture
}

output "console_bucket_name" {
  description = "Console bucket (s3_cloudfront only; null otherwise). The deploy workflow derives the same name."
  value       = one(module.console[*].bucket_name)
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID: set it as the repository variable CLOUDFRONT_DISTRIBUTION_ID (s3_cloudfront only; null otherwise)."
  value       = one(module.console[*].distribution_id)
}

output "app_url" {
  description = "Application URL: the CloudFront domain with s3_cloudfront, else the load balancer (null while asleep)."
  value       = local.console_s3 ? "https://${one(module.console[*].domain_name)}" : (length(module.alb) == 0 ? null : "http://${one(module.alb[*].dns_name)}")
}

output "excluded_zone_ids" {
  description = "Availability zone IDs the VPC avoids (use1-az3 with ARM64 in us-east-1)."
  value       = module.network.excluded_zone_ids
}

output "console_api_origin" {
  description = "Domain CloudFront sends API requests to (s3_cloudfront only; a non-resolving name while asleep)."
  value       = one(module.console[*].api_origin_domain)
}
