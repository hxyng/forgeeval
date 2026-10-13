output "state_bucket" {
  description = "Terraform state bucket; pass it to infra/envs/prod as -backend-config=bucket=..."
  value       = aws_s3_bucket.state.bucket
}

output "region" {
  description = "Region of the state bucket."
  value       = var.region
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider used by both roles."
  value       = local.oidc_provider_arn
}

output "plan_role_arn" {
  description = "Set as the repository variable AWS_PLAN_ROLE_ARN to enable the plan job on pull requests."
  value       = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  description = "Set as the repository (or production environment) variable AWS_DEPLOY_ROLE_ARN to enable deploys."
  value       = aws_iam_role.deploy.arn
}

output "backend_config" {
  description = "Contents for infra/envs/prod/backend.hcl."
  value       = "bucket = \"${aws_s3_bucket.state.bucket}\"\nregion = \"${var.region}\"\n"
}
