data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = coalesce(var.state_bucket_name, "${var.name_prefix}-tfstate-${local.account_id}")

  oidc_url          = "token.actions.githubusercontent.com"
  create_oidc       = var.existing_oidc_provider_arn == null
  oidc_provider_arn = local.create_oidc ? aws_iam_openid_connect_provider.github[0].arn : var.existing_oidc_provider_arn

  # Subjects GitHub puts in the OIDC token. Pull-request jobs get the first, jobs that declare
  # `environment: production` get the second. They are matched exactly (no wildcards).
  plan_subject   = "repo:${var.github_repository}:pull_request"
  deploy_subject = "repo:${var.github_repository}:environment:${var.github_environment}"

  arn_prefix = "arn:aws"
  ecs_arn    = "${local.arn_prefix}:ecs:${var.region}:${local.account_id}"
  cluster    = var.name_prefix
  # Names below mirror infra/modules/compute.
  repository_arns = [for r in ["backend", "web"] : "${local.arn_prefix}:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}-${r}"]
  service_arns    = [for s in ["api", "worker", "web"] : "${local.ecs_arn}:service/${local.cluster}/${var.name_prefix}-${s}"]
  pass_role_arns  = [for r in ["exec", "api-task", "worker-task"] : "${local.arn_prefix}:iam::${local.account_id}:role/${var.name_prefix}-${r}"]
  log_group_arn   = "${local.arn_prefix}:logs:${var.region}:${local.account_id}:log-group:/ecs/${var.name_prefix}"
}

# ---------------------------------------------------------------------------------------------
# Remote state bucket (S3-native locking: use_lockfile = true in the backend block)
# ---------------------------------------------------------------------------------------------

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state_tls_only" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  # Versioning must be on before a noncurrent-version rule is accepted.
  depends_on = [aws_s3_bucket_versioning.state]

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.state_noncurrent_version_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------------------------------------------------------------------------------------------
# GitHub Actions OIDC: no long-lived AWS keys anywhere
# ---------------------------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count = local.create_oidc ? 1 : 0

  url            = "https://${local.oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "plan" {
  name                 = "${var.name_prefix}-github-plan"
  description          = "Read-only role for terraform plan on pull requests"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_url}:aud" = "sts.amazonaws.com"
          "${local.oidc_url}:sub" = local.plan_subject
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "plan_read_only" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# Plans run with -lock=false, so the role only needs to read the state objects.
resource "aws_iam_role_policy" "plan_state_read" {
  name = "state-read"
  role = aws_iam_role.plan.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListStateBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.state.arn
      },
      {
        Sid      = "ReadState"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.state.arn}/*"
      },
    ]
  })
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name_prefix}-github-deploy"
  description          = "Assumed by the production deploy workflow only"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_url}:aud" = "sts.amazonaws.com"
          "${local.oidc_url}:sub" = local.deploy_subject
        }
      }
    }]
  })
}

# Exactly what .github/workflows/deploy.yml does: push two images, run the migrate task, register
# task definitions, update three services, read the migration logs.
resource "aws_iam_role_policy" "deploy" {
  name = "deploy"
  role = aws_iam_role.deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetAuthorizationToken does not support resource-level permissions.
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = local.repository_arns
      },
      {
        # Registering and describing task definitions do not support resource-level permissions.
        Sid      = "EcsTaskDefinitions"
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
        Resource = "*"
      },
      {
        Sid      = "EcsUpdateServices"
        Effect   = "Allow"
        Action   = ["ecs:DescribeServices", "ecs:UpdateService"]
        Resource = local.service_arns
      },
      {
        Sid       = "EcsRunMigration"
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = "${local.ecs_arn}:task-definition/${var.name_prefix}-migrate:*"
        Condition = { ArnEquals = { "ecs:cluster" = "${local.ecs_arn}:cluster/${local.cluster}" } }
      },
      {
        Sid      = "EcsWatchTasks"
        Effect   = "Allow"
        Action   = ["ecs:DescribeTasks", "ecs:StopTask"]
        Resource = "${local.ecs_arn}:task/${local.cluster}/*"
      },
      {
        Sid       = "PassTaskRolesToEcsOnly"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = local.pass_role_arns
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
      {
        Sid      = "ReadMigrationLogs"
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents", "logs:DescribeLogStreams"]
        Resource = ["${local.log_group_arn}/*", "${local.log_group_arn}/*:*"]
      },
      {
        # Describe* calls on load balancers do not support resource-level permissions; the smoke test
        # looks up the ALB DNS name.
        Sid      = "FindLoadBalancer"
        Effect   = "Allow"
        Action   = "elasticloadbalancing:DescribeLoadBalancers"
        Resource = "*"
      },
    ]
  })
}
