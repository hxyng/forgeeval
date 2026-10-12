output "alarm_topic_arn" {
  description = "SNS topic that receives every alarm."
  value       = aws_sns_topic.alarms.arn
}

output "alarm_names" {
  description = "Names of all alarms that exist for the current settings."
  value = concat(
    [
      aws_cloudwatch_metric_alarm.dlq_not_empty.alarm_name,
      aws_cloudwatch_metric_alarm.queue_oldest_message.alarm_name,
      aws_cloudwatch_metric_alarm.rds_cpu.alarm_name,
    ],
    aws_cloudwatch_metric_alarm.alb_5xx_rate[*].alarm_name,
    aws_cloudwatch_metric_alarm.api_unhealthy_targets[*].alarm_name,
    aws_cloudwatch_metric_alarm.rds_free_storage[*].alarm_name,
    aws_cloudwatch_metric_alarm.aurora_acu_utilization[*].alarm_name,
  )
}
