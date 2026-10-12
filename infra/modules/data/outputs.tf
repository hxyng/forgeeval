output "db_engine" {
  description = "Database engine in use (rds or aurora-serverless)."
  value       = var.database_engine
}

output "db_identifier" {
  description = "DB instance identifier (RDS instance, or the Aurora cluster's instance); the CloudWatch DBInstanceIdentifier dimension."
  value       = one(concat(aws_db_instance.this[*].identifier, aws_rds_cluster_instance.aurora[*].identifier))
}

output "db_cluster_identifier" {
  description = "Aurora cluster identifier (CloudWatch DBClusterIdentifier dimension); null with the rds engine."
  value       = one(aws_rds_cluster.aurora[*].cluster_identifier)
}

output "db_host" {
  description = "Database hostname (the Aurora cluster writer endpoint with aurora-serverless)."
  value       = one(concat(aws_db_instance.this[*].address, aws_rds_cluster.aurora[*].endpoint))
}

output "db_port" {
  description = "Database port."
  value       = one(concat(aws_db_instance.this[*].port, aws_rds_cluster.aurora[*].port))
}

output "db_name" {
  description = "Initial database name."
  value       = var.db_name
}

output "db_master_secret_arn" {
  description = "ARN of the Database-managed master secret (JSON keys username and password)."
  value       = one(concat(aws_db_instance.this[*].master_user_secret[0].secret_arn, aws_rds_cluster.aurora[*].master_user_secret[0].secret_arn))
}

output "queue_url" {
  description = "URL of the eval-jobs queue."
  value       = aws_sqs_queue.jobs.url
}

output "queue_arn" {
  description = "ARN of the eval-jobs queue."
  value       = aws_sqs_queue.jobs.arn
}

output "queue_name" {
  description = "Name of the eval-jobs queue (CloudWatch dimension)."
  value       = aws_sqs_queue.jobs.name
}

output "dlq_name" {
  description = "Name of the dead-letter queue (CloudWatch dimension)."
  value       = aws_sqs_queue.dlq.name
}

output "dlq_arn" {
  description = "ARN of the dead-letter queue."
  value       = aws_sqs_queue.dlq.arn
}

output "bucket_name" {
  description = "Payload bucket name."
  value       = aws_s3_bucket.payloads.bucket
}

output "bucket_arn" {
  description = "Payload bucket ARN."
  value       = aws_s3_bucket.payloads.arn
}

output "api_key_pepper_secret_arn" {
  description = "ARN of the API key pepper secret."
  value       = aws_secretsmanager_secret.api_key_pepper.arn
}

output "model_api_key_secret_arn" {
  description = "ARN of the model API key secret (value set out-of-band)."
  value       = aws_secretsmanager_secret.model_api_key.arn
}

output "model_api_key_secret_name" {
  description = "Name of the model API key secret, for aws secretsmanager put-secret-value."
  value       = aws_secretsmanager_secret.model_api_key.name
}
