# ---------------------------------------------------------------------------------------------
# Worker scaling from zero (worker_min = 0) or without Container Insights: step scaling on SQS only
#
# Why this scales out from 0 tasks: the alarm watches ApproximateNumberOfMessagesVisible, which SQS
# publishes whether or not any worker runs, and Application Auto Scaling may raise the desired count
# from 0 when its min_capacity is 0. Target tracking cannot: its backlog-per-task expression needs the
# running count, which has no datapoints at 0 tasks.
# Scale-in happens only when the queue holds no visible and no in-flight message for a while, so a
# busy worker is never stopped by scaling.
# ---------------------------------------------------------------------------------------------

resource "aws_appautoscaling_policy" "worker_scale_out" {
  count = var.services_enabled && local.step_scaling ? 1 : 0

  name               = "${var.name_prefix}-worker-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  resource_id        = aws_appautoscaling_target.worker[0].resource_id

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 120
    metric_aggregation_type = "Maximum"

    # Bounds are relative to the alarm threshold (1 visible message).
    step_adjustment {
      metric_interval_lower_bound = "0"
      metric_interval_upper_bound = "9"
      scaling_adjustment          = 1
    }

    step_adjustment {
      metric_interval_lower_bound = "9"
      metric_interval_upper_bound = "49"
      scaling_adjustment          = 2
    }

    step_adjustment {
      metric_interval_lower_bound = "49"
      scaling_adjustment          = 4
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_backlog" {
  count = var.services_enabled && local.step_scaling ? 1 : 0

  alarm_name          = "${var.name_prefix}-worker-backlog"
  alarm_description   = "Queue has visible messages: add workers (scales from zero)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_scale_out[0].arn]

  dimensions = {
    QueueName = var.queue_name
  }
}

resource "aws_appautoscaling_policy" "worker_scale_in" {
  count = var.services_enabled && local.step_scaling ? 1 : 0

  name               = "${var.name_prefix}-worker-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  resource_id        = aws_appautoscaling_target.worker[0].resource_id

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = "0"
      scaling_adjustment          = var.worker_min
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_idle" {
  count = var.services_enabled && local.step_scaling ? 1 : 0

  alarm_name          = "${var.name_prefix}-worker-idle"
  alarm_description   = "No visible and no in-flight messages for ${var.worker_idle_minutes} minutes: scale workers to the minimum."
  evaluation_periods  = var.worker_idle_minutes
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # An idle queue stops publishing metrics after a while; no data means nothing to do.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_appautoscaling_policy.worker_scale_in[0].arn]

  metric_query {
    id          = "total"
    expression  = "visible + inflight"
    label       = "Visible plus in-flight messages"
    return_data = true
  }

  metric_query {
    id = "visible"

    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      period      = 60
      stat        = "Maximum"
      dimensions  = { QueueName = var.queue_name }
    }
  }

  metric_query {
    id = "inflight"

    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      period      = 60
      stat        = "Maximum"
      dimensions  = { QueueName = var.queue_name }
    }
  }
}
