mock_provider "aws" {
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:t-alarms" }
  }
}

variables {
  name_prefix                 = "t"
  queue_name                  = "t-eval-jobs"
  dlq_name                    = "t-eval-jobs-dlq"
  alb_arn_suffix              = "app/t-alb/abc"
  api_target_group_arn_suffix = "targetgroup/t-api/def"
  db_identifier               = "t-db"
}

run "alarms_cover_the_failure_modes_and_notify" {
  assert {
    condition     = aws_cloudwatch_metric_alarm.dlq_not_empty.threshold == 0 && aws_cloudwatch_metric_alarm.dlq_not_empty.metric_name == "ApproximateNumberOfMessagesVisible"
    error_message = "DLQ alarm fires on any visible message."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.queue_oldest_message.threshold == 600
    error_message = "Oldest-message alarm fires after 10 minutes."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.rds_cpu.threshold == 80
    error_message = "RDS CPU alarm at 80%."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.api_unhealthy_targets.metric_name == "UnHealthyHostCount"
    error_message = "Unhealthy API targets alarm."
  }

  assert {
    condition     = length(output.alarm_names) == 6
    error_message = "Six alarms."
  }

  assert {
    condition = alltrue([
      for a in [
        aws_cloudwatch_metric_alarm.dlq_not_empty,
        aws_cloudwatch_metric_alarm.queue_oldest_message,
        aws_cloudwatch_metric_alarm.alb_5xx_rate,
        aws_cloudwatch_metric_alarm.api_unhealthy_targets,
        aws_cloudwatch_metric_alarm.rds_cpu,
        aws_cloudwatch_metric_alarm.rds_free_storage,
      ] : length(a.alarm_actions) == 1
    ])
    error_message = "Every alarm notifies the SNS topic."
  }

  assert {
    condition     = aws_kms_key.alarms.enable_key_rotation == true && length(aws_sns_topic_subscription.email) == 0
    error_message = "Topic key rotates; no email subscription unless an address is set."
  }
}

run "email_subscription_is_optional" {
  variables {
    alarm_email = "oncall@example.com"
  }

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 1
    error_message = "An address creates the subscription."
  }
}
