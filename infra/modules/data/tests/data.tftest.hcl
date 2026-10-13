mock_provider "aws" {
  mock_resource "aws_db_instance" {
    defaults = {
      address = "t-db.example.us-east-1.rds.amazonaws.com"
      port    = 5432
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-0000-AbCdEf"
        kms_key_id    = ""
        secret_status = "active"
      }]
    }
  }

  mock_resource "aws_rds_cluster" {
    defaults = {
      endpoint = "t-db.cluster-example.us-east-1.rds.amazonaws.com"
      port     = 5432
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!cluster-0000-AbCdEf"
        kms_key_id    = ""
        secret_status = "active"
      }]
    }
  }
}

variables {
  name_prefix           = "t"
  private_subnet_ids    = ["subnet-a", "subnet-b"]
  rds_security_group_id = "sg-rds"
}

run "database_is_private_encrypted_and_tls_only" {
  assert {
    condition     = aws_db_instance.this[0].publicly_accessible == false
    error_message = "RDS must not be publicly accessible."
  }

  assert {
    condition     = aws_db_instance.this[0].storage_encrypted == true && aws_db_instance.this[0].storage_type == "gp3"
    error_message = "RDS storage must be encrypted gp3."
  }

  assert {
    condition     = aws_db_instance.this[0].manage_master_user_password == true
    error_message = "The master password must be RDS-managed."
  }

  assert {
    condition     = one([for p in aws_db_parameter_group.this[0].parameter : p.value if p.name == "rds.force_ssl"]) == "1"
    error_message = "rds.force_ssl must be 1."
  }

  assert {
    condition     = aws_db_instance.this[0].backup_retention_period == 7 && aws_db_instance.this[0].deletion_protection == true && aws_db_instance.this[0].multi_az == false
    error_message = "Defaults: 7-day backups, deletion protection on, single AZ."
  }

  assert {
    condition     = aws_db_instance.this[0].instance_class == "db.t4g.micro" && aws_db_instance.this[0].engine == "postgres" && startswith(aws_db_instance.this[0].engine_version, "16")
    error_message = "PostgreSQL 16 on db.t4g.micro by default."
  }
}

run "queue_semantics_match_the_adr" {
  assert {
    condition     = aws_sqs_queue.jobs.visibility_timeout_seconds == 120 && aws_sqs_queue.jobs.receive_wait_time_seconds == 20
    error_message = "Visibility timeout 120 s and 20 s long polling."
  }

  assert {
    condition     = jsondecode(aws_sqs_queue.jobs.redrive_policy).maxReceiveCount == 5
    error_message = "Redrive after 5 receives."
  }

  assert {
    condition     = aws_sqs_queue.dlq.message_retention_seconds == 1209600
    error_message = "DLQ retains messages for 14 days."
  }

  assert {
    condition     = aws_sqs_queue.jobs.sqs_managed_sse_enabled == true && aws_sqs_queue.dlq.sqs_managed_sse_enabled == true
    error_message = "Both queues use server-side encryption."
  }

  assert {
    condition = alltrue([
      for k, p in aws_sqs_queue_policy.tls_only :
      jsondecode(p.policy).Statement[0].Effect == "Deny" && jsondecode(p.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    ])
    error_message = "Both queue policies deny non-TLS access."
  }
}

run "payload_bucket_is_locked_down" {
  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.payloads.block_public_acls,
      aws_s3_bucket_public_access_block.payloads.block_public_policy,
      aws_s3_bucket_public_access_block.payloads.ignore_public_acls,
      aws_s3_bucket_public_access_block.payloads.restrict_public_buckets,
    ])
    error_message = "All four S3 public access blocks must be on."
  }

  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.payloads.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    error_message = "Bucket must use server-side encryption."
  }

  assert {
    condition     = jsondecode(aws_s3_bucket_policy.payloads_tls_only.policy).Statement[0].Effect == "Deny"
    error_message = "Bucket policy must deny non-TLS requests."
  }

  assert {
    condition     = one(aws_s3_bucket_lifecycle_configuration.payloads.rule).transition[*].days == tolist([30]) && one(aws_s3_bucket_lifecycle_configuration.payloads.rule).expiration[0].days == 365
    error_message = "STANDARD_IA at 30 days, expiry at payload_retention_days (365)."
  }
}

run "retention_must_exceed_ia_transition" {
  command = plan

  variables {
    payload_retention_days = 20
  }

  expect_failures = [var.payload_retention_days]
}

run "rds_is_the_default_engine_and_no_aurora_resources_exist" {
  assert {
    condition     = length(aws_db_instance.this) == 1 && length(aws_rds_cluster.aurora) == 0 && length(aws_rds_cluster_instance.aurora) == 0 && length(aws_rds_cluster_parameter_group.aurora) == 0
    error_message = "Default engine rds creates one instance and no Aurora resources."
  }

  assert {
    condition     = output.db_cluster_identifier == null && output.db_identifier == "t-db" && output.db_engine == "rds"
    error_message = "RDS contract: instance identifier, no cluster identifier."
  }
}

run "aurora_serverless_scales_to_zero_and_stays_private_and_encrypted" {
  variables {
    database_engine = "aurora-serverless"
  }

  assert {
    condition     = length(aws_db_instance.this) == 0 && length(aws_db_parameter_group.this) == 0 && length(aws_rds_cluster.aurora) == 1 && length(aws_rds_cluster_instance.aurora) == 1
    error_message = "Aurora replaces the RDS instance: one cluster, one instance."
  }

  assert {
    condition     = aws_rds_cluster.aurora[0].engine == "aurora-postgresql" && startswith(aws_rds_cluster.aurora[0].engine_version, "16.")
    error_message = "Aurora PostgreSQL 16."
  }

  assert {
    condition     = one(aws_rds_cluster.aurora[0].serverlessv2_scaling_configuration).min_capacity == 0 && one(aws_rds_cluster.aurora[0].serverlessv2_scaling_configuration).max_capacity == 2 && one(aws_rds_cluster.aurora[0].serverlessv2_scaling_configuration).seconds_until_auto_pause == 300
    error_message = "Scales to 0 ACU, max 2 ACU, pauses after 300 s idle."
  }

  assert {
    condition     = aws_rds_cluster_instance.aurora[0].instance_class == "db.serverless" && aws_rds_cluster_instance.aurora[0].publicly_accessible == false
    error_message = "One private db.serverless instance."
  }

  assert {
    condition     = aws_rds_cluster.aurora[0].storage_encrypted == true && aws_rds_cluster.aurora[0].manage_master_user_password == true
    error_message = "Encrypted storage and an Aurora-managed master secret."
  }

  assert {
    condition     = one([for p in aws_rds_cluster_parameter_group.aurora[0].parameter : p.value if p.name == "rds.force_ssl"]) == "1" && aws_rds_cluster_parameter_group.aurora[0].family == "aurora-postgresql16"
    error_message = "Cluster parameter group forces TLS."
  }

  assert {
    condition     = aws_rds_cluster.aurora[0].db_cluster_parameter_group_name == aws_rds_cluster_parameter_group.aurora[0].name && aws_rds_cluster.aurora[0].db_subnet_group_name == aws_db_subnet_group.this.name && aws_rds_cluster.aurora[0].vpc_security_group_ids == toset(["sg-rds"])
    error_message = "Cluster uses the TLS parameter group, the private subnet group and the database security group."
  }

  assert {
    condition     = aws_db_subnet_group.this.subnet_ids == toset(["subnet-a", "subnet-b"]) && aws_rds_cluster_instance.aurora[0].db_subnet_group_name == aws_db_subnet_group.this.name
    error_message = "Aurora lives in the private subnets."
  }

  assert {
    condition     = aws_rds_cluster.aurora[0].backup_retention_period == 7 && aws_rds_cluster.aurora[0].deletion_protection == true
    error_message = "7-day backups and deletion protection on by default."
  }

  assert {
    condition     = output.db_cluster_identifier == "t-db" && output.db_identifier == "t-db-1" && output.db_host == "t-db.cluster-example.us-east-1.rds.amazonaws.com" && output.db_port == 5432 && output.db_name == "forgeeval" && startswith(output.db_master_secret_arn, "arn:aws:secretsmanager")
    error_message = "Aurora keeps the module output contract."
  }
}

run "aurora_deletion_protection_follows_the_variable" {
  variables {
    database_engine     = "aurora-serverless"
    deletion_protection = false
    aurora_max_acu      = 4
  }

  assert {
    condition     = aws_rds_cluster.aurora[0].deletion_protection == false && one(aws_rds_cluster.aurora[0].serverlessv2_scaling_configuration).max_capacity == 4
    error_message = "deletion_protection and aurora_max_acu are wired through."
  }
}

run "aurora_engine_older_than_16_3_is_rejected" {
  command = plan

  variables {
    database_engine       = "aurora-serverless"
    aurora_engine_version = "16.2"
  }

  expect_failures = [var.aurora_engine_version]
}

run "unknown_engine_is_rejected" {
  command = plan

  variables {
    database_engine = "dynamodb"
  }

  expect_failures = [var.database_engine]
}
