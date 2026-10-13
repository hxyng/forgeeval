# Whole-environment composition test with a mocked AWS provider: nothing here talks to AWS.

mock_provider "aws" {
  mock_resource "aws_db_instance" {
    defaults = {
      address = "forgeeval-prod-db.example.us-east-1.rds.amazonaws.com"
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
      endpoint = "forgeeval-prod-db.cluster-example.us-east-1.rds.amazonaws.com"
      port     = 5432
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!cluster-0000-AbCdEf"
        kms_key_id    = ""
        secret_status = "active"
      }]
    }
  }

  # What a deploy registered after Terraform's own bootstrap revision (the latest ACTIVE revision).
  mock_data "aws_ecs_task_definition" {
    defaults = {
      arn      = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock-deployed:42"
      revision = 42
    }
  }

  mock_resource "aws_lb" {
    defaults = {
      arn        = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/forgeeval-prod-alb/50dc6c495c0c9188"
      arn_suffix = "app/forgeeval-prod-alb/50dc6c495c0c9188"
      dns_name   = "forgeeval-prod-alb-1.us-east-1.elb.amazonaws.com"
    }
  }

  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/1111111111111111" }
  }

  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/mock/1/2" }
  }

  mock_resource "aws_appautoscaling_policy" {
    defaults = { arn = "arn:aws:autoscaling:us-east-1:123456789012:scalingPolicy:mock:resource/ecs/service/t/t-worker:policyName/mock" }
  }

  mock_resource "aws_cloudfront_distribution" {
    defaults = {
      arn         = "arn:aws:cloudfront::123456789012:distribution/E1ABCDEF"
      domain_name = "d111111abcdef8.cloudfront.net"
    }
  }

  mock_resource "aws_cloudfront_function" {
    defaults = { arn = "arn:aws:cloudfront::123456789012:function/mock" }
  }

  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/mock" }
  }

  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/forgeeval-prod" }
  }

  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }

  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-east-1:123456789012:repository/mock"
      repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock"
    }
  }

  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:123456789012:mock"
      url = "https://sqs.us-east-1.amazonaws.com/123456789012/mock"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }

  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock-AbCdEf" }
  }

  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
}

override_data {
  target = module.network.data.aws_availability_zones.available
  values = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
}

# A mocked account id would otherwise be random per run and make cross-run comparisons meaningless.
override_data {
  target = module.data.data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}

override_data {
  target = module.observability.data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}

run "defaults_keep_tasks_public_and_the_database_private" {
  assert {
    condition     = module.compute.service_subnet_ids == toset(module.network.public_subnet_ids)
    error_message = "With the NAT gateway off, services must run in the public subnets (and only those)."
  }

  assert {
    condition     = module.compute.service_assign_public_ip == true
    error_message = "Public subnets need public IPs for tasks to reach ECR and AWS APIs."
  }

  assert {
    condition     = length(setintersection(module.compute.service_subnet_ids, toset(module.network.private_subnet_ids))) == 0
    error_message = "No service may land in a private subnet in this mode."
  }

  assert {
    condition     = output.assign_public_ip == true && output.task_subnet_ids == module.network.public_subnet_ids
    error_message = "run-task outputs must match the services' network placement."
  }
}

run "nat_gateway_moves_tasks_to_private_subnets" {
  variables {
    enable_nat_gateway = true
  }

  assert {
    condition     = module.compute.service_subnet_ids == toset(module.network.private_subnet_ids) && module.compute.service_assign_public_ip == false
    error_message = "With NAT, services run in private subnets without public IPs."
  }
}

run "https_certificate_switches_the_alb_to_https" {
  variables {
    acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/abc"
  }

  assert {
    condition     = module.alb[0].https_enabled == true
    error_message = "A certificate enables the HTTPS listener."
  }
}

run "outputs_expose_what_run_task_needs" {
  assert {
    condition = alltrue([
      output.alb_dns_name != "",
      output.ecs_cluster_name == "forgeeval-prod",
      output.migrate_task_definition_family == "forgeeval-prod-migrate",
      output.api_service_name == "forgeeval-prod-api",
      output.worker_service_name == "forgeeval-prod-worker",
      output.web_service_name == "forgeeval-prod-web",
      output.task_security_group_id != "",
      length(output.task_subnet_ids) == 2,
      toset(keys(output.ecr_repository_urls)) == toset(["backend", "web"]),
    ])
    error_message = "Outputs the deploy workflow and runbook depend on."
  }
}

run "invalid_name_prefix_is_rejected" {
  command = plan

  variables {
    name_prefix = "Bad_Name"
  }

  expect_failures = [var.name_prefix]
}

# --- demo mode (active, database_engine) ----------------------------------------------------

run "default_is_awake_on_rds_with_the_full_alarm_set" {
  assert {
    condition     = var.active == true && var.database_engine == "rds" && length(module.alb) == 1
    error_message = "Defaults: awake, RDS, one load balancer module."
  }

  assert {
    condition     = output.alb_dns_name != null && output.api_service_name != null && output.worker_service_name != null && output.web_service_name != null
    error_message = "Awake: load balancer and services exist."
  }

  assert {
    condition     = length(module.observability.alarm_names) == 6 && module.data.db_engine == "rds" && module.data.db_cluster_identifier == null
    error_message = "Unchanged default: six alarms (queue 2, load balancer 2, database 2) on RDS."
  }

  assert {
    condition     = module.data.db_identifier == "forgeeval-prod-db"
    error_message = "RDS instance identifier is unchanged."
  }
}

run "asleep_removes_the_load_balancer_and_services_but_nothing_persistent" {
  variables {
    active = false
  }

  assert {
    condition     = length(module.alb) == 0 && output.alb_dns_name == null
    error_message = "No load balancer, listeners, target groups or listener rules while asleep."
  }

  assert {
    condition     = output.api_service_name == null && output.worker_service_name == null && output.web_service_name == null && output.active == false
    error_message = "No ECS services while asleep."
  }

  assert {
    condition     = length(module.observability.alarm_names) == 4
    error_message = "Alarms referencing the load balancer are gone; queue and database alarms stay."
  }

  assert {
    condition     = output.ecs_cluster_name == "forgeeval-prod" && output.migrate_task_definition_family == "forgeeval-prod-migrate" && toset(keys(output.ecr_repository_urls)) == toset(["backend", "web"]) && output.db_master_secret_arn != null && output.alarm_topic_arn != null
    error_message = "Cluster, task definitions, ECR, database secret and alarm topic persist."
  }

  assert {
    condition     = output.task_subnet_ids != null && output.task_security_group_id != ""
    error_message = "Network outputs needed by run-task persist."
  }
}

run "toggling_active_does_not_change_persistent_resources" {
  variables {
    active = false
  }

  assert {
    condition     = output.persistent_state == run.default_is_awake_on_rds_with_the_full_alarm_set.persistent_state
    error_message = "Everything that survives sleep must be identical with active = true and false."
  }
}

run "services_start_from_the_latest_deployed_revision_when_woken" {
  assert {
    condition     = alltrue([for k in ["api", "web", "worker"] : module.compute.service_latest_task_definition_arns[k] == "arn:aws:ecs:us-east-1:123456789012:task-definition/mock-deployed:42"])
    error_message = "Waking recreates services from the latest ACTIVE revision of each family."
  }
}

run "aurora_engine_keeps_the_module_contract_and_swaps_the_storage_alarm" {
  variables {
    database_engine = "aurora-serverless"
  }

  assert {
    condition     = module.data.db_engine == "aurora-serverless" && module.data.db_cluster_identifier == "forgeeval-prod-db" && module.data.db_identifier == "forgeeval-prod-db-1"
    error_message = "Aurora cluster and instance identifiers."
  }

  assert {
    condition     = output.db_master_secret_arn == "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!cluster-0000-AbCdEf"
    error_message = "The managed master secret flows through the same output."
  }

  assert {
    condition     = length(module.observability.alarm_names) == 6 && contains(module.observability.alarm_names, "forgeeval-prod-aurora-acu-high") && !contains(module.observability.alarm_names, "forgeeval-prod-rds-free-storage-low")
    error_message = "Aurora: ACU utilization replaces free-storage."
  }
}

run "aurora_gets_a_longer_health_check_grace_period" {
  variables {
    database_engine = "aurora-serverless"
  }

  assert {
    condition     = module.compute.service_health_check_grace_periods["api"] == 180 && module.compute.service_health_check_grace_periods["web"] == 120
    error_message = "Aurora resume (15 s, 30 s+ after a day) needs a longer grace period."
  }
}

run "asleep_on_aurora_is_valid_too" {
  variables {
    active          = false
    database_engine = "aurora-serverless"
  }

  assert {
    condition     = length(module.alb) == 0 && length(module.observability.alarm_names) == 4
    error_message = "Both switches combine: Aurora asleep."
  }
}

# --- lean always-on options -------------------------------------------------------------------

run "lean_defaults_change_nothing" {
  assert {
    condition = (
      output.cpu_architecture == "X86_64" &&
      alltrue([for k, a in module.compute.task_runtime_platforms : a == "X86_64"]) &&
      module.compute.service_capacity_providers == { api = [], web = [], worker = [] } &&
      module.compute.service_desired_counts == { api = 2, web = 1, worker = 1 } &&
      module.compute.worker_scaling == { mode = "target-tracking", min = 1, max = 5 } &&
      module.compute.container_insights == "enabled" &&
      output.console_hosting == "ecs" &&
      length(module.console) == 0 &&
      output.cloudfront_distribution_id == null &&
      length(module.network.excluded_zone_ids) == 0
    )
    error_message = "Without the lean variables the environment is exactly what it was: x86, on-demand, api 2 / web 1 / worker 1, target tracking, Insights on, console on ECS."
  }

  assert {
    condition     = module.alb[0].web_target_group_arn != null && output.app_url == "http://forgeeval-prod-alb-1.us-east-1.elb.amazonaws.com"
    error_message = "Default URL is the load balancer."
  }
}

run "arm64_applies_to_every_task_definition_and_skips_the_unsupported_zone" {
  variables {
    cpu_architecture = "ARM64"
  }

  assert {
    condition     = length(module.compute.task_runtime_platforms) == 4 && alltrue([for k, a in module.compute.task_runtime_platforms : a == "ARM64"])
    error_message = "api, worker, web and migrate are all ARM64."
  }

  assert {
    condition     = module.network.excluded_zone_ids == toset(["use1-az3"])
    error_message = "Fargate has no ARM64 capacity in use1-az3: the VPC must avoid it."
  }
}

run "arm64_outside_us_east_1_excludes_no_zone" {
  variables {
    cpu_architecture = "ARM64"
    region           = "eu-west-1"
  }

  assert {
    condition     = length(module.network.excluded_zone_ids) == 0
    error_message = "The exclusion is specific to us-east-1."
  }
}

run "fargate_spot_is_for_the_worker_only" {
  variables {
    worker_capacity = "FARGATE_SPOT"
  }

  assert {
    condition     = module.compute.service_capacity_providers == { api = [], web = [], worker = ["FARGATE_SPOT"] }
    error_message = "Only the worker is pinned to FARGATE_SPOT."
  }
}

run "worker_can_scale_to_zero" {
  variables {
    worker_min_count = 0
  }

  assert {
    condition     = module.compute.worker_scaling == { mode = "step", min = 0, max = 5 }
    error_message = "worker_min_count = 0 uses step scaling on queue backlog with a minimum of 0."
  }
}

run "worker_min_above_max_is_rejected" {
  command = plan

  variables {
    worker_min_count = 6
  }

  expect_failures = [var.worker_min_count]
}

run "api_can_run_a_single_task" {
  variables {
    api_desired_count = 1
  }

  assert {
    condition     = module.compute.service_desired_counts["api"] == 1
    error_message = "One API task."
  }
}

run "zero_api_tasks_is_rejected" {
  command = plan

  variables {
    api_desired_count = 0
  }

  expect_failures = [var.api_desired_count]
}

run "container_insights_off" {
  variables {
    container_insights = false
  }

  assert {
    condition     = module.compute.container_insights == "disabled" && module.compute.worker_scaling.mode == "step" && module.compute.worker_scaling.min == 1
    error_message = "Insights off: the cluster setting is disabled and worker scaling no longer depends on its metrics."
  }
}

run "console_on_s3_and_cloudfront_drops_the_web_tier" {
  variables {
    console_hosting = "s3_cloudfront"
  }

  assert {
    condition     = output.web_service_name == null && !contains(keys(module.compute.task_runtime_platforms), "web") && !contains(keys(module.compute.service_desired_counts), "web")
    error_message = "No web service or task definition."
  }

  assert {
    condition     = module.alb[0].web_target_group_arn == null
    error_message = "No web target group."
  }

  assert {
    condition     = length(module.console) == 1 && output.console_bucket_name != null && output.cloudfront_distribution_id != null && startswith(output.app_url, "https://")
    error_message = "The bucket and distribution exist and the application URL is the HTTPS CloudFront domain."
  }

  assert {
    condition     = output.console_api_origin == "forgeeval-prod-alb-1.us-east-1.elb.amazonaws.com"
    error_message = "CloudFront sends /v1/* to the load balancer."
  }

  assert {
    condition     = output.api_service_name != null && output.worker_service_name != null
    error_message = "api and worker are unaffected."
  }
}

run "console_on_cloudfront_cannot_be_combined_with_an_alb_certificate" {
  command = plan

  variables {
    console_hosting     = "s3_cloudfront"
    acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/abc"
  }

  expect_failures = [var.console_hosting]
}

run "invalid_console_hosting_is_rejected" {
  command = plan

  variables {
    console_hosting = "cdn"
  }

  expect_failures = [var.console_hosting]
}

run "invalid_worker_capacity_is_rejected" {
  command = plan

  variables {
    worker_capacity = "EC2"
  }

  expect_failures = [var.worker_capacity]
}

# --- lean options with demo mode ---------------------------------------------------------------

run "awake_lean_reference" {
  variables {
    console_hosting    = "s3_cloudfront"
    cpu_architecture   = "ARM64"
    worker_capacity    = "FARGATE_SPOT"
    worker_min_count   = 0
    api_desired_count  = 1
    container_insights = false
  }

  assert {
    condition     = module.compute.service_desired_counts["api"] == 1 && !contains(keys(module.compute.service_desired_counts), "web") && module.compute.worker_scaling.min == 0 && length(module.console) == 1
    error_message = "Lean awake: api 1, worker 0, no web, console on CloudFront."
  }
}

run "asleep_with_the_console_on_cloudfront_keeps_the_bucket_and_distribution" {
  variables {
    active             = false
    console_hosting    = "s3_cloudfront"
    cpu_architecture   = "ARM64"
    worker_capacity    = "FARGATE_SPOT"
    worker_min_count   = 0
    api_desired_count  = 1
    container_insights = false
  }

  assert {
    condition     = length(module.alb) == 0 && output.alb_dns_name == null
    error_message = "Asleep: no load balancer, even with the lean options."
  }

  assert {
    condition     = output.api_service_name == null && output.worker_service_name == null && output.web_service_name == null && module.compute.service_desired_counts == {} && module.compute.worker_scaling == null
    error_message = "Asleep: no ECS services and no worker autoscaling."
  }

  assert {
    condition     = length(module.console) == 1 && output.console_bucket_name != null && output.cloudfront_distribution_id != null
    error_message = "The bucket and the distribution persist while asleep."
  }

  assert {
    condition     = output.console_api_origin == "asleep.invalid" && output.app_url != null
    error_message = "The console URL keeps working; the API origin points at a name that does not resolve."
  }

  assert {
    condition     = output.persistent_state == run.awake_lean_reference.persistent_state
    error_message = "Everything that survives sleep, including the console bucket and distribution, is identical awake and asleep."
  }
}
