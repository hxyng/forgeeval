data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------------------------
# PostgreSQL 16: RDS instance (default) or Aurora Serverless v2 (var.database_engine)
# ---------------------------------------------------------------------------------------------

locals {
  rds    = var.database_engine == "rds"
  aurora = var.database_engine == "aurora-serverless"
}

# The RDS resources gained a count (engine switch); these keep existing state addresses stable.
moved {
  from = aws_db_parameter_group.this
  to   = aws_db_parameter_group.this[0]
}

moved {
  from = aws_db_instance.this
  to   = aws_db_instance.this[0]
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_parameter_group" "this" {
  count = local.rds ? 1 : 0

  name_prefix = "${var.name_prefix}-pg16-"
  family      = "postgres16"
  description = "ForgeEval PostgreSQL 16: TLS required"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  lifecycle { create_before_destroy = true }
}

resource "aws_db_instance" "this" {
  count = local.rds ? 1 : 0

  identifier     = "${var.name_prefix}-db"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  storage_type          = "gp3"
  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  # RDS creates and rotates the master password in Secrets Manager; it never enters Terraform state.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.rds_security_group_id]
  parameter_group_name   = aws_db_parameter_group.this[0].name
  publicly_accessible    = false
  multi_az               = var.multi_az

  backup_retention_period = var.backup_retention_days
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot   = true

  auto_minor_version_upgrade          = true
  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql", "upgrade"]

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${var.name_prefix}-db-final"

  lifecycle {
    # RDS applies minor upgrades itself; do not fight it.
    ignore_changes = [engine_version]
  }
}

# Aurora PostgreSQL Serverless v2 that scales to 0 ACU and pauses when idle (demo mode). Only the
# instance-hours stop: storage and backups still bill while paused. Auto-pause needs engine 16.3+.
resource "aws_rds_cluster_parameter_group" "aurora" {
  count = local.aurora ? 1 : 0

  name_prefix = "${var.name_prefix}-aurora-pg16-"
  family      = "aurora-postgresql16"
  description = "ForgeEval Aurora PostgreSQL 16: TLS required"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  lifecycle { create_before_destroy = true }
}

resource "aws_rds_cluster" "aurora" {
  count = local.aurora ? 1 : 0

  cluster_identifier = "${var.name_prefix}-db"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned" # Serverless v2 runs in provisioned mode with db.serverless instances
  engine_version     = var.aurora_engine_version

  database_name   = var.db_name
  master_username = var.db_username
  # Aurora creates and rotates the master password in Secrets Manager; it never enters Terraform state.
  manage_master_user_password = true

  storage_encrypted               = true
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [var.rds_security_group_id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.aurora[0].name

  backup_retention_period = var.backup_retention_days
  preferred_backup_window = "03:00-04:00"
  copy_tags_to_snapshot   = true

  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${var.name_prefix}-db-final"

  serverlessv2_scaling_configuration {
    min_capacity             = 0
    max_capacity             = var.aurora_max_acu
    seconds_until_auto_pause = var.aurora_auto_pause_seconds
  }

  lifecycle {
    # Aurora applies minor upgrades itself; do not fight it.
    ignore_changes = [engine_version]
  }
}

resource "aws_rds_cluster_instance" "aurora" {
  count = local.aurora ? 1 : 0

  identifier                 = "${var.name_prefix}-db-1"
  cluster_identifier         = aws_rds_cluster.aurora[0].id
  engine                     = aws_rds_cluster.aurora[0].engine
  engine_version             = aws_rds_cluster.aurora[0].engine_version
  instance_class             = "db.serverless"
  db_subnet_group_name       = aws_db_subnet_group.this.name
  publicly_accessible        = false
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true

  lifecycle {
    ignore_changes = [engine_version]
  }
}

# ---------------------------------------------------------------------------------------------
# SQS: eval-jobs with a dead-letter queue (docs/adr/0001-job-queue.md)
# ---------------------------------------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name_prefix}-eval-jobs-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.name_prefix}-eval-jobs"
  visibility_timeout_seconds = var.queue_visibility_timeout_seconds
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.queue_max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

resource "aws_sqs_queue_policy" "tls_only" {
  for_each = { jobs = aws_sqs_queue.jobs, dlq = aws_sqs_queue.dlq }

  queue_url = each.value.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = each.value.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ---------------------------------------------------------------------------------------------
# S3 payload bucket (large trace payloads under traces/)
# ---------------------------------------------------------------------------------------------

resource "aws_s3_bucket" "payloads" {
  bucket        = "${var.name_prefix}-payloads-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.payload_force_destroy
}

resource "aws_s3_bucket_ownership_controls" "payloads" {
  bucket = aws_s3_bucket.payloads.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "payloads" {
  bucket = aws_s3_bucket.payloads.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "payloads" {
  bucket = aws_s3_bucket.payloads.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "payloads_tls_only" {
  bucket = aws_s3_bucket.payloads.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.payloads.arn, "${aws_s3_bucket.payloads.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.payloads]
}

resource "aws_s3_bucket_lifecycle_configuration" "payloads" {
  bucket = aws_s3_bucket.payloads.id

  rule {
    id     = "age-out-payloads"
    status = "Enabled"

    filter {}

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = var.payload_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------------------------------------------------------------------------------------------
# Secrets Manager (the database credentials secret is created and rotated by RDS)
# ---------------------------------------------------------------------------------------------

# Generated at apply time and never stored in Terraform state: the value is ephemeral and goes to
# Secrets Manager through a write-only argument.
ephemeral "random_password" "api_key_pepper" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "api_key_pepper" {
  name                    = "${var.name_prefix}/api-key-pepper"
  description             = "Pepper mixed into API key hashes. Changing it invalidates every issued key."
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "api_key_pepper" {
  secret_id                = aws_secretsmanager_secret.api_key_pepper.id
  secret_string_wo         = ephemeral.random_password.api_key_pepper.result
  secret_string_wo_version = 1
}

resource "aws_secretsmanager_secret" "model_api_key" {
  name                    = "${var.name_prefix}/model-api-key"
  description             = "API key for the OpenAI-compatible model endpoint. Set out-of-band; see infra/README.md."
  recovery_window_in_days = var.secret_recovery_window_days
}

# ECS cannot start a task whose secret has no value, so the secret carries a placeholder. The real
# value is written with `aws secretsmanager put-secret-value`; Terraform ignores later changes.
resource "aws_secretsmanager_secret_version" "model_api_key" {
  secret_id     = aws_secretsmanager_secret.model_api_key.id
  secret_string = "unset-run-put-secret-value"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
