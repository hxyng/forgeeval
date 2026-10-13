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
}

variables {
  name_prefix           = "t"
  private_subnet_ids    = ["subnet-a", "subnet-b"]
  rds_security_group_id = "sg-rds"
}

run "database_is_private_encrypted_and_tls_only" {
  assert {
    condition     = aws_db_instance.this.publicly_accessible == false
    error_message = "RDS must not be publicly accessible."
  }

  assert {
    condition     = aws_db_instance.this.storage_encrypted == true && aws_db_instance.this.storage_type == "gp3"
    error_message = "RDS storage must be encrypted gp3."
  }

  assert {
    condition     = aws_db_instance.this.manage_master_user_password == true
    error_message = "The master password must be RDS-managed."
  }

  assert {
    condition     = one([for p in aws_db_parameter_group.this.parameter : p.value if p.name == "rds.force_ssl"]) == "1"
    error_message = "rds.force_ssl must be 1."
  }

  assert {
    condition     = aws_db_instance.this.backup_retention_period == 7 && aws_db_instance.this.deletion_protection == true && aws_db_instance.this.multi_az == false
    error_message = "Defaults: 7-day backups, deletion protection on, single AZ."
  }

  assert {
    condition     = aws_db_instance.this.instance_class == "db.t4g.micro" && aws_db_instance.this.engine == "postgres" && startswith(aws_db_instance.this.engine_version, "16")
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
