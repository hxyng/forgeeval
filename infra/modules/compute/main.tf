locals {
  services = toset(["api", "worker", "web", "migrate"])

  # Long-running services that exist only while the environment is awake (demo mode).
  running_services = var.services_enabled ? toset(["api", "worker", "web"]) : toset([])

  backend_image = "${aws_ecr_repository.this["backend"].repository_url}:${var.image_tag}"
  web_image     = "${aws_ecr_repository.this["web"].repository_url}:${var.image_tag}"

  # The application builds its database URL from these parts (backend/forgeeval/config.py).
  db_environment = [
    { name = "DB_HOST", value = var.db_host },
    { name = "DB_PORT", value = tostring(var.db_port) },
    { name = "DB_NAME", value = var.db_name },
  ]

  # The one-off migrate task gets credentials injected at start ("<arn>:<key>::"): it lives for
  # seconds, so password rotation can't strand it. Long-running api/worker tasks instead read
  # the secret per connection via DB_SECRET_ARN (backend/forgeeval/db/credentials.py), because
  # RDS rotates the master password every 7 days and ECS injects secrets only at task start.
  db_secrets = [
    { name = "DB_USERNAME", valueFrom = "${var.db_master_secret_arn}:username::" },
    { name = "DB_PASSWORD", valueFrom = "${var.db_master_secret_arn}:password::" },
  ]

  backend_environment = concat(
    [
      { name = "FORGEEVAL_ENV", value = "production" },
      { name = "QUEUE_BACKEND", value = "sqs" },
      { name = "SQS_QUEUE_URL", value = var.queue_url },
      { name = "AWS_REGION", value = var.region },
      { name = "S3_BUCKET", value = var.bucket_name },
      { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://localhost:4318" },
      { name = "DB_SECRET_ARN", value = var.db_master_secret_arn },
    ],
    local.db_environment,
    var.model_base_url == null ? [] : [{ name = "MODEL_BASE_URL", value = var.model_base_url }],
  )

  backend_secrets = [
    { name = "API_KEY_PEPPER", valueFrom = var.api_key_pepper_secret_arn },
    { name = "MODEL_API_KEY", valueFrom = var.model_api_key_secret_arn },
  ]

  # Collector config: OTLP in, traces to X-Ray, metrics to CloudWatch as EMF log events.
  adot_config = yamlencode({
    receivers = {
      otlp = { protocols = { grpc = { endpoint = "0.0.0.0:4317" }, http = { endpoint = "0.0.0.0:4318" } } }
    }
    processors = { batch = {} }
    exporters = {
      awsxray = { region = var.region }
      awsemf = {
        region                           = var.region
        namespace                        = var.metrics_namespace
        log_group_name                   = aws_cloudwatch_log_group.emf.name
        resource_to_telemetry_conversion = { enabled = true }
        dimension_rollup_option          = "NoDimensionRollup"
      }
    }
    service = {
      pipelines = {
        traces  = { receivers = ["otlp"], processors = ["batch"], exporters = ["awsxray"] }
        metrics = { receivers = ["otlp"], processors = ["batch"], exporters = ["awsemf"] }
      }
    }
  })

  # Sidecar shared by the three long-running backend roles. Not essential: a collector crash
  # must not take the application down.
  adot_container = {
    name      = "adot"
    image     = var.adot_image
    essential = false
    environment = [
      { name = "AOT_CONFIG_CONTENT", value = local.adot_config },
      { name = "AWS_REGION", value = var.region },
    ]
  }

  task_definitions = {
    api = {
      cpu           = var.api_cpu
      memory        = var.api_memory
      task_role_arn = aws_iam_role.api_task.arn
      containers = [
        {
          name         = "api"
          image        = local.backend_image
          essential    = true
          command      = ["uvicorn", "forgeeval.api.main:app", "--host", "0.0.0.0", "--port", tostring(var.api_port)]
          portMappings = [{ containerPort = var.api_port, protocol = "tcp" }]
          environment  = local.backend_environment
          secrets      = local.backend_secrets
          stopTimeout  = 30
          healthCheck = {
            command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.api_port}/healthz', timeout=3)\" || exit 1"]
            interval    = 15
            timeout     = 5
            retries     = 3
            startPeriod = 30
          }
        },
        local.adot_container,
      ]
    }
    worker = {
      cpu           = var.worker_cpu
      memory        = var.worker_memory
      task_role_arn = aws_iam_role.worker_task.arn
      containers = [
        {
          name        = "worker"
          image       = local.backend_image
          essential   = true
          command     = ["python", "-m", "forgeeval.worker.main"]
          environment = local.backend_environment
          secrets     = local.backend_secrets
          # Give an in-flight job the full visibility window to finish after SIGTERM (Fargate maximum).
          stopTimeout = 120
        },
        local.adot_container,
      ]
    }
    web = {
      cpu           = var.web_cpu
      memory        = var.web_memory
      task_role_arn = null
      containers = [
        {
          name         = "web"
          image        = local.web_image
          essential    = true
          portMappings = [{ containerPort = var.web_port, protocol = "tcp" }]
          # Fallback proxy target only: the ALB already routes /v1/* to the API, so it must not depend on
          # the ALB (whose DNS name changes whenever demo mode recreates it).
          environment = [{ name = "API_UPSTREAM", value = var.api_upstream }]
          secrets     = []
          stopTimeout = 30
        },
      ]
    }
    migrate = {
      cpu           = var.migrate_cpu
      memory        = var.migrate_memory
      task_role_arn = null
      # No collector sidecar: the task must exit when `alembic upgrade head` finishes.
      containers = [
        {
          name      = "migrate"
          image     = local.backend_image
          essential = true
          command   = ["alembic", "upgrade", "head"]
          environment = concat(
            [{ name = "FORGEEVAL_ENV", value = "production" }],
            local.db_environment,
          )
          secrets = local.db_secrets
        },
      ]
    }
  }
}

# ---------------------------------------------------------------------------------------------
# Logs and cluster
# ---------------------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "service" {
  for_each = local.services

  name              = "/ecs/${var.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "emf" {
  name              = "/${var.name_prefix}/emf"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "this" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------------------------------------------------------------------------------------------
# Task definitions. Services ignore later revisions: the deploy workflow registers a revision per
# commit and points the services at it.
# ---------------------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "this" {
  for_each = local.task_definitions

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = each.value.task_role_arn
  skip_destroy             = true # keep old revisions so a manual rollback stays possible

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    for c in each.value.containers : merge(
      { for k, v in c : k => v if v != null },
      {
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.service[each.key].name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = c.name
          }
        }
      },
    )
  ])
}

# ---------------------------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------------------------

# The services (and everything that needs a load balancer) exist only while var.services_enabled.
# Task definitions, the cluster, queues and the database persist while the environment sleeps.
moved {
  from = terraform_data.alb_ready
  to   = terraform_data.alb_ready[0]
}

moved {
  from = aws_ecs_service.api
  to   = aws_ecs_service.api[0]
}

moved {
  from = aws_ecs_service.web
  to   = aws_ecs_service.web[0]
}

moved {
  from = aws_ecs_service.worker
  to   = aws_ecs_service.worker[0]
}

moved {
  from = aws_appautoscaling_target.worker
  to   = aws_appautoscaling_target.worker[0]
}

moved {
  from = aws_appautoscaling_policy.worker_backlog
  to   = aws_appautoscaling_policy.worker_backlog[0]
}

# A service that is (re)created starts from the newest ACTIVE revision of its family, i.e. the
# image the last deploy rolled out, not the "bootstrap" revision Terraform registered. Querying by
# family name alone returns the latest ACTIVE revision. Services created earlier ignore it
# (ignore_changes below), so this only matters on creation.
data "aws_ecs_task_definition" "latest" {
  for_each = local.running_services

  task_definition = aws_ecs_task_definition.this[each.key].family
}

# Target groups must be attached to a listener before a service can register with them.
resource "terraform_data" "alb_ready" {
  count = var.services_enabled ? 1 : 0

  input = var.alb_listener_arns
}

resource "aws_ecs_service" "api" {
  count = var.services_enabled ? 1 : 0

  name                               = "${var.name_prefix}-api"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = data.aws_ecs_task_definition.latest["api"].arn
  desired_count                      = var.api_desired_count
  launch_type                        = "FARGATE"
  enable_execute_command             = var.enable_ecs_exec
  health_check_grace_period_seconds  = var.api_health_check_grace_period_seconds
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [var.tasks_security_group_id]
    assign_public_ip = var.assign_public_ip
  }

  load_balancer {
    target_group_arn = var.api_target_group_arn
    container_name   = "api"
    container_port   = var.api_port
  }

  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [terraform_data.alb_ready]
}

resource "aws_ecs_service" "web" {
  count = var.services_enabled ? 1 : 0

  name                               = "${var.name_prefix}-web"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = data.aws_ecs_task_definition.latest["web"].arn
  desired_count                      = var.web_desired_count
  launch_type                        = "FARGATE"
  enable_execute_command             = var.enable_ecs_exec
  health_check_grace_period_seconds  = var.web_health_check_grace_period_seconds
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [var.tasks_security_group_id]
    assign_public_ip = var.assign_public_ip
  }

  load_balancer {
    target_group_arn = var.web_target_group_arn
    container_name   = "web"
    container_port   = var.web_port
  }

  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [terraform_data.alb_ready]
}

resource "aws_ecs_service" "worker" {
  count = var.services_enabled ? 1 : 0

  name                               = "${var.name_prefix}-worker"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = data.aws_ecs_task_definition.latest["worker"].arn
  desired_count                      = var.worker_min
  launch_type                        = "FARGATE"
  enable_execute_command             = var.enable_ecs_exec
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [var.tasks_security_group_id]
    assign_public_ip = var.assign_public_ip
  }

  lifecycle {
    # Application Auto Scaling owns the count; the deploy workflow owns the task definition.
    ignore_changes = [task_definition, desired_count]
  }
}

# ---------------------------------------------------------------------------------------------
# Worker scaling: keep queue backlog per running worker near the target
# ---------------------------------------------------------------------------------------------

resource "aws_appautoscaling_target" "worker" {
  count = var.services_enabled ? 1 : 0

  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker[0].name}"
  min_capacity       = var.worker_min
  max_capacity       = var.worker_max
}

resource "aws_appautoscaling_policy" "worker_backlog" {
  count = var.services_enabled ? 1 : 0

  name               = "${var.name_prefix}-worker-backlog"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  resource_id        = aws_appautoscaling_target.worker[0].resource_id

  target_tracking_scaling_policy_configuration {
    target_value       = var.worker_backlog_per_task
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    customized_metric_specification {
      metrics {
        id          = "backlog"
        expression  = "visible / MAX([running, 1])"
        label       = "Visible messages per running worker task"
        return_data = true
      }

      metrics {
        id          = "visible"
        return_data = false

        metric_stat {
          stat = "Average"

          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesVisible"

            dimensions {
              name  = "QueueName"
              value = var.queue_name
            }
          }
        }
      }

      metrics {
        id          = "running"
        return_data = false

        metric_stat {
          stat = "Average"

          metric {
            namespace   = "ECS/ContainerInsights"
            metric_name = "RunningTaskCount"

            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.this.name
            }

            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.worker[0].name
            }
          }
        }
      }
    }
  }
}
