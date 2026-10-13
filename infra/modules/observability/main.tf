data "aws_caller_identity" "current" {}

locals {
  alb_enabled    = var.alb_enabled
  aurora_enabled = var.db_engine == "aurora-serverless"
  account_id     = data.aws_caller_identity.current.account_id
  actions        = [aws_sns_topic.alarms.arn]
}

# CloudWatch cannot publish to a topic encrypted with the AWS-managed SNS key, so the topic gets
# its own customer-managed key whose policy admits the CloudWatch service.
resource "aws_kms_key" "alarms" {
  description             = "${var.name_prefix} alarm topic"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        # In a key policy "*" means "this key".
        Resource = "*"
      },
      {
        Sid       = "CloudWatchAlarmsPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
    ]
  })
}

resource "aws_kms_alias" "alarms" {
  name          = "alias/${var.name_prefix}-alarms"
  target_key_id = aws_kms_key.alarms.key_id
}

resource "aws_sns_topic" "alarms" {
  name              = "${var.name_prefix}-alarms"
  kms_master_key_id = aws_kms_key.alarms.arn
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ---------------------------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name_prefix}-dlq-not-empty"
  alarm_description   = "Messages reached the dead-letter queue: a job failed to be processed five times (worker crash loop?). See docs/runbooks/deploy-and-rollback.md."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.dlq_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

resource "aws_cloudwatch_metric_alarm" "queue_oldest_message" {
  alarm_name          = "${var.name_prefix}-queue-oldest-message"
  alarm_description   = "The oldest job has waited longer than ${var.queue_age_threshold_seconds}s: workers are not keeping up or are down."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = var.queue_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = var.queue_age_threshold_seconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# ---------------------------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "alb_5xx_rate" {
  count = local.alb_enabled ? 1 : 0

  alarm_name          = "${var.name_prefix}-alb-5xx-rate"
  alarm_description   = "More than ${var.alb_5xx_rate_threshold_percent}% of requests through the load balancer returned 5xx."
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alb_5xx_rate_threshold_percent
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions

  metric_query {
    id          = "rate"
    expression  = "IF(requests > 0, 100 * (FILL(elb5xx, 0) + FILL(target5xx, 0)) / requests, 0)"
    label       = "5xx rate (%)"
    return_data = true
  }

  metric_query {
    id = "requests"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = { LoadBalancer = var.alb_arn_suffix }
      period      = 300
      stat        = "Sum"
    }
  }

  metric_query {
    id = "elb5xx"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      dimensions  = { LoadBalancer = var.alb_arn_suffix }
      period      = 300
      stat        = "Sum"
    }
  }

  metric_query {
    id = "target5xx"

    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      dimensions  = { LoadBalancer = var.alb_arn_suffix }
      period      = 300
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "api_unhealthy_targets" {
  count = local.alb_enabled ? 1 : 0

  alarm_name          = "${var.name_prefix}-api-unhealthy-targets"
  alarm_description   = "At least one API task is failing its /readyz health check."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = var.alb_arn_suffix, TargetGroup = var.api_target_group_arn_suffix }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# ---------------------------------------------------------------------------------------------
# Database. With the load balancer gone (demo mode asleep) the two alarms above do not exist; the
# queue and database alarms always do.
# ---------------------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name_prefix}-rds-cpu-high"
  alarm_description   = "Database CPU above ${var.db_cpu_threshold_percent}% for 10 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.db_cpu_threshold_percent
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  count = local.aurora_enabled ? 0 : 1

  alarm_name          = "${var.name_prefix}-rds-free-storage-low"
  alarm_description   = "Database free storage is below ${var.db_free_storage_threshold_bytes} bytes."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.db_free_storage_threshold_bytes
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}

# Aurora has no fixed volume to fill (storage grows to 128 TiB on its own), so FreeStorageSpace
# does not apply. The useful capacity signal is ACUUtilization: sustained use near the configured
# maximum means the database is capped and queries are being starved. It reports 0 while paused.
resource "aws_cloudwatch_metric_alarm" "aurora_acu_utilization" {
  count = local.aurora_enabled ? 1 : 0

  alarm_name          = "${var.name_prefix}-aurora-acu-high"
  alarm_description   = "Aurora Serverless v2 capacity is above ${var.aurora_acu_utilization_threshold_percent}% of its maximum ACUs for 10 minutes: raise aurora_max_acu."
  namespace           = "AWS/RDS"
  metric_name         = "ACUUtilization"
  dimensions          = { DBClusterIdentifier = var.db_cluster_identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.aurora_acu_utilization_threshold_percent
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "missing"
  alarm_actions       = local.actions
  ok_actions          = local.actions
}
