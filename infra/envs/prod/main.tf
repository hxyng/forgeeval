module "network" {
  source = "../../modules/network"

  name_prefix        = var.name_prefix
  vpc_cidr           = var.vpc_cidr
  enable_nat_gateway = var.enable_nat_gateway
}

module "data" {
  source = "../../modules/data"

  name_prefix            = var.name_prefix
  private_subnet_ids     = module.network.private_subnet_ids
  rds_security_group_id  = module.network.rds_security_group_id
  db_instance_class      = var.db_instance_class
  db_allocated_storage   = var.db_allocated_storage
  multi_az               = var.multi_az
  deletion_protection    = var.deletion_protection
  payload_retention_days = var.payload_retention_days
  payload_force_destroy  = var.teardown_force_delete
}

module "alb" {
  source = "../../modules/alb"

  name_prefix         = var.name_prefix
  vpc_id              = module.network.vpc_id
  public_subnet_ids   = module.network.public_subnet_ids
  security_group_id   = module.network.alb_security_group_id
  acm_certificate_arn = var.acm_certificate_arn
  access_logs_bucket  = var.alb_access_logs_bucket
}

module "compute" {
  source = "../../modules/compute"

  name_prefix = var.name_prefix
  region      = var.region

  task_subnet_ids         = module.network.task_subnet_ids
  assign_public_ip        = module.network.assign_public_ip
  tasks_security_group_id = module.network.tasks_security_group_id
  api_target_group_arn    = module.alb.api_target_group_arn
  web_target_group_arn    = module.alb.web_target_group_arn
  alb_dns_name            = module.alb.dns_name
  alb_listener_arns       = module.alb.listener_arns

  queue_url                 = module.data.queue_url
  queue_arn                 = module.data.queue_arn
  queue_name                = module.data.queue_name
  bucket_name               = module.data.bucket_name
  bucket_arn                = module.data.bucket_arn
  db_host                   = module.data.db_host
  db_port                   = module.data.db_port
  db_name                   = module.data.db_name
  db_master_secret_arn      = module.data.db_master_secret_arn
  api_key_pepper_secret_arn = module.data.api_key_pepper_secret_arn
  model_api_key_secret_arn  = module.data.model_api_key_secret_arn
  model_base_url            = var.model_base_url

  image_tag         = var.image_tag
  api_desired_count = var.api_desired_count
  worker_max        = var.worker_max
  enable_ecs_exec   = var.enable_ecs_exec
  ecr_force_delete  = var.teardown_force_delete
}

module "observability" {
  source = "../../modules/observability"

  name_prefix                 = var.name_prefix
  alarm_email                 = var.alarm_email
  queue_name                  = module.data.queue_name
  dlq_name                    = module.data.dlq_name
  alb_arn_suffix              = module.alb.arn_suffix
  api_target_group_arn_suffix = module.alb.api_target_group_arn_suffix
  db_identifier               = module.data.db_identifier
}
