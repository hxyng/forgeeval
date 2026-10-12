mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/mock" }
  }

  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/t" }
  }

  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }

  # The newest ACTIVE revision of a family: a deploy has registered revision 42 since Terraform's own.
  mock_data "aws_ecs_task_definition" {
    defaults = {
      arn      = "arn:aws:ecs:us-east-1:123456789012:task-definition/t-deployed:42"
      revision = 42
    }
  }
}

override_resource {
  target = aws_ecr_repository.this["backend"]
  values = {
    arn            = "arn:aws:ecr:us-east-1:123456789012:repository/t-backend"
    repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/t-backend"
  }
}

override_resource {
  target = aws_ecr_repository.this["web"]
  values = {
    arn            = "arn:aws:ecr:us-east-1:123456789012:repository/t-web"
    repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/t-web"
  }
}

variables {
  name_prefix               = "t"
  region                    = "us-east-1"
  task_subnet_ids           = ["subnet-a", "subnet-b"]
  assign_public_ip          = true
  tasks_security_group_id   = "sg-tasks"
  api_target_group_arn      = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/t-api/1111111111111111"
  web_target_group_arn      = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/t-web/2222222222222222"
  api_upstream              = "http://127.0.0.1:8000"
  alb_listener_arns         = ["arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/t-alb/1/2"]
  queue_url                 = "https://sqs.us-east-1.amazonaws.com/123456789012/t-eval-jobs"
  queue_arn                 = "arn:aws:sqs:us-east-1:123456789012:t-eval-jobs"
  queue_name                = "t-eval-jobs"
  bucket_name               = "t-payloads"
  bucket_arn                = "arn:aws:s3:::t-payloads"
  db_host                   = "t-db.example.us-east-1.rds.amazonaws.com"
  db_name                   = "forgeeval"
  db_master_secret_arn      = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-0000-AbCdEf"
  api_key_pepper_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:t/api-key-pepper-AbCdEf"
  model_api_key_secret_arn  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:t/model-api-key-AbCdEf"
}

run "services_roll_back_automatically_and_exec_is_off" {
  assert {
    condition = alltrue([
      for s in [aws_ecs_service.api[0], aws_ecs_service.web[0], aws_ecs_service.worker[0]] :
      one(s.deployment_circuit_breaker).enable == true && one(s.deployment_circuit_breaker).rollback == true && s.enable_execute_command == false
    ])
    error_message = "Every service needs the circuit breaker with rollback and ECS Exec off."
  }

  assert {
    condition     = aws_ecs_service.api[0].desired_count == 2 && aws_ecs_service.web[0].desired_count == 1 && aws_ecs_service.worker[0].desired_count == 1
    error_message = "Defaults: api 2, web 1, worker 1."
  }

  assert {
    condition     = one(aws_ecs_cluster.this.setting).name == "containerInsights" && one(aws_ecs_cluster.this.setting).value == "enabled"
    error_message = "Container Insights must be on."
  }

  assert {
    condition     = one(aws_ecs_service.api[0].load_balancer).container_port == 8000 && one(aws_ecs_service.web[0].load_balancer).container_port == 80 && length(aws_ecs_service.worker[0].load_balancer) == 0
    error_message = "api and web register with the ALB; the worker has no port."
  }

  assert {
    condition     = aws_appautoscaling_target.worker[0].min_capacity == 1 && aws_appautoscaling_target.worker[0].max_capacity == 5
    error_message = "Worker scales between 1 and worker_max."
  }

  assert {
    condition     = aws_appautoscaling_policy.worker_backlog[0].policy_type == "TargetTrackingScaling"
    error_message = "Worker scaling is target tracking on queue backlog."
  }
}

run "tasks_follow_the_network_module_output" {
  assert {
    condition     = output.service_subnet_ids == toset(var.task_subnet_ids) && output.service_assign_public_ip == true
    error_message = "Services use exactly the subnets and public-IP setting handed in by the network module."
  }

  assert {
    condition     = alltrue([for s in [aws_ecs_service.api[0], aws_ecs_service.web[0], aws_ecs_service.worker[0]] : one(s.network_configuration).security_groups == toset(["sg-tasks"])])
    error_message = "All services share the tasks security group."
  }
}

run "private_mode_has_no_public_ips" {
  variables {
    assign_public_ip = false
    task_subnet_ids  = ["subnet-private-a", "subnet-private-b"]
  }

  assert {
    condition     = output.service_assign_public_ip == false && output.service_subnet_ids == toset(["subnet-private-a", "subnet-private-b"])
    error_message = "NAT mode: private subnets, no public IPs."
  }
}

run "task_definitions_follow_the_app_contract" {
  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["api"].container_definitions)[0].command == ["uvicorn", "forgeeval.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    error_message = "api command."
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["worker"].container_definitions)[0].command == ["python", "-m", "forgeeval.worker.main"] && jsondecode(aws_ecs_task_definition.this["worker"].container_definitions)[0].stopTimeout == 120
    error_message = "worker command and 120 s stop timeout."
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["migrate"].container_definitions)[0].command == ["alembic", "upgrade", "head"] && length(jsondecode(aws_ecs_task_definition.this["migrate"].container_definitions)) == 1
    error_message = "migrate runs alembic alone, with no sidecar."
  }

  assert {
    condition     = one([for e in jsondecode(aws_ecs_task_definition.this["web"].container_definitions)[0].environment : e.value if e.name == "API_UPSTREAM"]) == "http://127.0.0.1:8000"
    error_message = "API_UPSTREAM is the static fallback (it must not reference the ALB)."
  }

  assert {
    condition = alltrue([
      for role in ["api", "worker"] : alltrue([
        contains([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.name], "QUEUE_BACKEND"),
        contains([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.name], "SQS_QUEUE_URL"),
        contains([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.name], "S3_BUCKET"),
        one([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.value if e.name == "FORGEEVAL_ENV"]) == "production",
        one([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.value if e.name == "QUEUE_BACKEND"]) == "sqs",
        one([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.value if e.name == "OTEL_EXPORTER_OTLP_ENDPOINT"]) == "http://localhost:4318",
        !contains([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.name], "AWS_ENDPOINT_URL"),
        !contains([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.name], "S3_ENDPOINT_URL"),
        !contains([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.name], "MODEL_BASE_URL"),
      ])
    ])
    error_message = "Backend environment contract (and no emulator endpoints in AWS)."
  }

  assert {
    condition = alltrue([
      for role in ["api", "worker"] :
      one([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].environment : e.value if e.name == "DB_SECRET_ARN"]) == var.db_master_secret_arn &&
      !contains([for s in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].secrets : s.name], "DB_PASSWORD") &&
      contains([for st in jsondecode(role == "api" ? aws_iam_role_policy.api_task.policy : aws_iam_role_policy.worker_task.policy).Statement : st.Action if st.Sid == "ReadDatabaseSecret" && st.Resource == var.db_master_secret_arn], "secretsmanager:GetSecretValue") &&
      one([for s in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].secrets : s.valueFrom if s.name == "API_KEY_PEPPER"]) == var.api_key_pepper_secret_arn &&
      one([for s in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[0].secrets : s.valueFrom if s.name == "MODEL_API_KEY"]) == var.model_api_key_secret_arn
    ])
    error_message = "api/worker read DB credentials per connection (DB_SECRET_ARN + task-role GetSecretValue), so rotation can't strand them; other secrets are injected."
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_ecs_task_definition.this["migrate"].container_definitions)[0].secrets : s.valueFrom if s.name == "DB_PASSWORD"]) == "${var.db_master_secret_arn}:password::"
    )
    error_message = "The short-lived migrate task gets DB credentials injected from the RDS-managed secret's JSON keys."
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.this["api"].container_definitions)[0].image == "123456789012.dkr.ecr.us-east-1.amazonaws.com/t-backend:bootstrap" && jsondecode(aws_ecs_task_definition.this["web"].container_definitions)[0].image == "123456789012.dkr.ecr.us-east-1.amazonaws.com/t-web:bootstrap"
    error_message = "Backend and web images come from their own ECR repositories."
  }
}

run "backend_tasks_carry_an_adot_sidecar" {
  assert {
    condition = alltrue([
      for role in ["api", "worker"] : alltrue([
        jsondecode(aws_ecs_task_definition.this[role].container_definitions)[1].name == "adot",
        startswith(jsondecode(aws_ecs_task_definition.this[role].container_definitions)[1].image, "public.ecr.aws/aws-observability/aws-otel-collector:v"),
        jsondecode(aws_ecs_task_definition.this[role].container_definitions)[1].essential == false,
        strcontains(one([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[1].environment : e.value if e.name == "AOT_CONFIG_CONTENT"]), "awsxray"),
        strcontains(one([for e in jsondecode(aws_ecs_task_definition.this[role].container_definitions)[1].environment : e.value if e.name == "AOT_CONFIG_CONTENT"]), "awsemf"),
      ])
    ])
    error_message = "api and worker get a non-essential ADOT collector exporting to X-Ray and CloudWatch EMF."
  }
}

run "iam_is_least_privilege" {
  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.execution, aws_iam_role_policy.api_task, aws_iam_role_policy.worker_task] :
      alltrue([for s in jsondecode(p.policy).Statement : s.Resource != "*" || contains(["EcrAuthToken", "XRayExport", "PutApplicationMetrics"], s.Sid)])
    ])
    error_message = "Resource * is allowed only for the three APIs that cannot be scoped."
  }

  assert {
    condition     = length(flatten([for s in jsondecode(aws_iam_role_policy.execution.policy).Statement : s.Resource if s.Sid == "ReadInjectedSecrets"])) == 3
    error_message = "The execution role reads exactly the three injected secrets."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.execution.policy).Statement : !contains(try(tolist(s.Action), [s.Action]), "secretsmanager:*")
    ])
    error_message = "No wildcard secret access."
  }

  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.api_task.policy).Statement : s.Action if s.Sid == "EnqueueJobs"])) == toset(["sqs:SendMessage", "sqs:GetQueueAttributes"]) &&
      toset(flatten([for s in jsondecode(aws_iam_role_policy.worker_task.policy).Statement : s.Action if s.Sid == "ConsumeAndRequeueJobs"])) == toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:SendMessage", "sqs:GetQueueAttributes"])
    )
    error_message = "SQS permissions per role."
  }

  assert {
    condition = alltrue([
      for p in [aws_iam_role_policy.api_task, aws_iam_role_policy.worker_task] :
      one([for s in jsondecode(p.policy).Statement : s.Resource if s.Sid == "ReadWritePayloads"]) == "arn:aws:s3:::t-payloads/traces/*" &&
      one([for s in jsondecode(p.policy).Statement : s.Condition.StringEquals["cloudwatch:namespace"] if s.Sid == "PutApplicationMetrics"]) == "ForgeEval"
    ])
    error_message = "S3 access is limited to traces/*; PutMetricData is limited to the namespace."
  }

  assert {
    condition     = alltrue([for p in [aws_iam_role_policy.api_task, aws_iam_role_policy.worker_task] : !contains([for s in jsondecode(p.policy).Statement : s.Sid], "EcsExecChannel")])
    error_message = "No ECS Exec permissions unless it is enabled."
  }
}

run "ecs_exec_is_opt_in" {
  variables {
    enable_ecs_exec = true
  }

  assert {
    condition     = aws_ecs_service.api[0].enable_execute_command == true && contains([for s in jsondecode(aws_iam_role_policy.api_task.policy).Statement : s.Sid], "EcsExecChannel")
    error_message = "enable_ecs_exec turns on both the flag and the permissions."
  }
}

run "registries_are_immutable_and_scanned" {
  assert {
    condition     = alltrue([for r in aws_ecr_repository.this : r.image_tag_mutability == "IMMUTABLE" && one(r.image_scanning_configuration).scan_on_push == true])
    error_message = "ECR: immutable tags and scan on push."
  }

  assert {
    condition     = alltrue([for p in aws_ecr_lifecycle_policy.this : jsondecode(p.policy).rules[0].selection.countNumber == 20])
    error_message = "ECR keeps the last 20 images."
  }

  assert {
    condition     = toset(keys(aws_ecr_repository.this)) == toset(["backend", "web"])
    error_message = "Two repositories: backend and web."
  }

  assert {
    condition     = alltrue([for g in aws_cloudwatch_log_group.service : g.retention_in_days == 30]) && aws_cloudwatch_log_group.emf.retention_in_days == 30
    error_message = "30-day log retention."
  }
}

run "model_base_url_is_plain_env_when_set" {
  variables {
    model_base_url = "https://models.example.com/v1"
  }

  assert {
    condition     = one([for e in jsondecode(aws_ecs_task_definition.this["worker"].container_definitions)[0].environment : e.value if e.name == "MODEL_BASE_URL"]) == "https://models.example.com/v1"
    error_message = "MODEL_BASE_URL is passed through as a plain variable."
  }
}

run "recreated_services_start_from_the_latest_deployed_revision" {
  assert {
    condition     = alltrue([for k in ["api", "web", "worker"] : data.aws_ecs_task_definition.latest[k].task_definition == "t-${k}"])
    error_message = "The lookup is by family name only, which returns the newest ACTIVE revision."
  }

  assert {
    condition     = alltrue([for s in [aws_ecs_service.api[0], aws_ecs_service.web[0], aws_ecs_service.worker[0]] : s.task_definition == "arn:aws:ecs:us-east-1:123456789012:task-definition/t-deployed:42"])
    error_message = "Services take their task definition from the latest-revision lookup, not Terraform's bootstrap revision."
  }
}

run "health_check_grace_periods_are_configurable" {
  variables {
    api_health_check_grace_period_seconds = 180
    web_health_check_grace_period_seconds = 120
  }

  assert {
    condition     = aws_ecs_service.api[0].health_check_grace_period_seconds == 180 && aws_ecs_service.web[0].health_check_grace_period_seconds == 120
    error_message = "Grace periods are wired through."
  }
}

run "default_grace_periods_are_unchanged" {
  assert {
    condition     = aws_ecs_service.api[0].health_check_grace_period_seconds == 90 && aws_ecs_service.web[0].health_check_grace_period_seconds == 60
    error_message = "Defaults: api 90 s, web 60 s."
  }
}

run "asleep_has_no_services_or_scaling_but_keeps_cluster_and_task_definitions" {
  variables {
    services_enabled     = false
    api_target_group_arn = null
    web_target_group_arn = null
    alb_listener_arns    = []
  }

  assert {
    condition     = length(aws_ecs_service.api) == 0 && length(aws_ecs_service.web) == 0 && length(aws_ecs_service.worker) == 0
    error_message = "No services while asleep."
  }

  assert {
    condition     = length(aws_appautoscaling_target.worker) == 0 && length(aws_appautoscaling_policy.worker_backlog) == 0 && length(terraform_data.alb_ready) == 0 && length(data.aws_ecs_task_definition.latest) == 0
    error_message = "No autoscaling, readiness gate or revision lookup while asleep."
  }

  assert {
    condition     = toset(keys(aws_ecs_task_definition.this)) == toset(["api", "worker", "web", "migrate"]) && aws_ecs_cluster.this.name == "t" && length(aws_cloudwatch_log_group.service) == 4 && length(aws_ecr_repository.this) == 2
    error_message = "Task definitions, cluster, log groups and ECR persist."
  }

  assert {
    condition     = output.api_service_name == null && output.worker_service_name == null && output.web_service_name == null && output.service_subnet_ids == null && output.service_assign_public_ip == null
    error_message = "Service outputs are null while asleep."
  }
}

run "awake_reference" {
  assert {
    condition     = length(output.task_definition_fingerprints) == 4
    error_message = "Reference fingerprints of the four task definitions while awake."
  }
}

run "task_definitions_do_not_depend_on_services_enabled" {
  variables {
    services_enabled = false
  }

  assert {
    condition     = output.task_definition_fingerprints == run.awake_reference.task_definition_fingerprints
    error_message = "Task definition content is identical awake and asleep."
  }
}
