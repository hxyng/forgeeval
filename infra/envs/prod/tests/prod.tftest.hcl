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
