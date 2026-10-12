data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  ecs_tasks_trust = {
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  }

  # Statements both application roles need.
  shared_task_statements = concat(
    [
      {
        Sid      = "ReadWritePayloads"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${var.bucket_arn}/traces/*"
      },
      {
        # The X-Ray APIs do not support resource-level permissions.
        Sid      = "XRayExport"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:GetSamplingStatisticSummaries"]
        Resource = "*"
      },
      {
        # PutMetricData has no resource-level permissions; the namespace condition is the scope.
        Sid       = "PutApplicationMetrics"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = var.metrics_namespace } }
      },
      {
        Sid      = "WriteEmfLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.emf.arn}:*"
      },
    ],
    var.enable_ecs_exec ? [{
      # ECS Exec needs the SSM messages channel; these actions have no resource-level permissions.
      Sid      = "EcsExecChannel"
      Effect   = "Allow"
      Action   = ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"]
      Resource = "*"
    }] : [],
  )
}

# ---------------------------------------------------------------------------------------------
# Task execution role: pulls images, writes container logs, reads exactly these secrets
# ---------------------------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-exec"
  assume_role_policy = jsonencode(local.ecs_tasks_trust)
}

resource "aws_iam_role_policy" "execution" {
  name = "execution"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetAuthorizationToken does not support resource-level permissions.
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "PullImages"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [for r in aws_ecr_repository.this : r.arn]
      },
      {
        Sid      = "WriteContainerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for g in aws_cloudwatch_log_group.service : "${g.arn}:*"]
      },
      {
        Sid    = "ReadInjectedSecrets"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = [
          var.db_master_secret_arn,
          var.api_key_pepper_secret_arn,
          var.model_api_key_secret_arn,
        ]
      },
    ]
  })
}

# ---------------------------------------------------------------------------------------------
# Task roles (one per application role)
# ---------------------------------------------------------------------------------------------

resource "aws_iam_role" "api_task" {
  name               = "${var.name_prefix}-api-task"
  assume_role_policy = jsonencode(local.ecs_tasks_trust)
}

resource "aws_iam_role_policy" "api_task" {
  name = "api"
  role = aws_iam_role.api_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([{
      Sid      = "EnqueueJobs"
      Effect   = "Allow"
      Action   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
      Resource = var.queue_arn
    }], local.shared_task_statements)
  })
}

resource "aws_iam_role" "worker_task" {
  name               = "${var.name_prefix}-worker-task"
  assume_role_policy = jsonencode(local.ecs_tasks_trust)
}

resource "aws_iam_role_policy" "worker_task" {
  name = "worker"
  role = aws_iam_role.worker_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([{
      Sid      = "ConsumeAndRequeueJobs"
      Effect   = "Allow"
      Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:SendMessage", "sqs:GetQueueAttributes"]
      Resource = var.queue_arn
    }], local.shared_task_statements)
  })
}
