mock_provider "aws" {
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }

  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }

  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
}

run "state_bucket_is_versioned_encrypted_and_private" {
  assert {
    condition     = one(aws_s3_bucket_versioning.state.versioning_configuration).status == "Enabled"
    error_message = "State bucket must be versioned."
  }

  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    error_message = "State bucket must be encrypted."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.state.block_public_acls,
      aws_s3_bucket_public_access_block.state.block_public_policy,
      aws_s3_bucket_public_access_block.state.ignore_public_acls,
      aws_s3_bucket_public_access_block.state.restrict_public_buckets,
    ])
    error_message = "State bucket must block all public access."
  }

  assert {
    condition     = jsondecode(aws_s3_bucket_policy.state_tls_only.policy).Statement[0].Effect == "Deny"
    error_message = "State bucket must deny non-TLS requests."
  }
}

run "deploy_role_trusts_only_the_production_environment" {
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:hxyng/forgeeval:environment:production"
    error_message = "The deploy role must be assumable only by repo:hxyng/forgeeval:environment:production."
  }

  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "The audience must be pinned to sts.amazonaws.com."
  }

  assert {
    condition     = !can(jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringLike) && length(jsondecode(aws_iam_role.deploy.assume_role_policy).Statement) == 1
    error_message = "No wildcard (StringLike) matching and a single trust statement."
  }

  assert {
    condition     = !strcontains(jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"], "*")
    error_message = "The subject must not contain wildcards."
  }
}

run "plan_role_trusts_pull_requests_and_is_read_only" {
  assert {
    condition     = jsondecode(aws_iam_role.plan.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:hxyng/forgeeval:pull_request"
    error_message = "The plan role is for pull_request jobs only."
  }

  assert {
    condition     = aws_iam_role_policy_attachment.plan_read_only.policy_arn == "arn:aws:iam::aws:policy/ReadOnlyAccess"
    error_message = "The plan role is read-only."
  }

  assert {
    condition     = alltrue([for s in jsondecode(aws_iam_role_policy.plan_state_read.policy).Statement : !contains(flatten([s.Action]), "s3:PutObject") && !contains(flatten([s.Action]), "s3:DeleteObject")])
    error_message = "The plan role must not write the state bucket."
  }

  assert {
    condition = alltrue([
      contains([for st in jsondecode(aws_iam_role_policy.plan_deny_data_reads.policy).Statement : st.Effect], "Deny"),
      contains(flatten([for st in jsondecode(aws_iam_role_policy.plan_deny_data_reads.policy).Statement : st.Action if st.Sid == "DenyObjectReadsOutsideState"]), "s3:GetObject"),
      one([for st in jsondecode(aws_iam_role_policy.plan_deny_data_reads.policy).Statement : st.NotResource if st.Sid == "DenyObjectReadsOutsideState"]) == "${aws_s3_bucket.state.arn}/*",
      alltrue([for a in ["secretsmanager:GetSecretValue", "sqs:ReceiveMessage", "logs:GetLogEvents"] : contains(flatten([for st in jsondecode(aws_iam_role_policy.plan_deny_data_reads.policy).Statement : st.Action if st.Sid == "DenyDataPlaneReads"]), a)]),
    ])
    error_message = "The plan role must not read customer data: S3 objects outside the state bucket, secret values, queue messages or log contents."
  }
}

run "deploy_policy_is_scoped_to_the_deploy_workflow" {
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      s.Resource != "*" || contains(["EcrLogin", "EcsTaskDefinitions", "FindLoadBalancer"], s.Sid)
    ])
    error_message = "Resource * is allowed only for APIs without resource-level permissions."
  }

  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Condition.StringEquals["iam:PassedToService"] if s.Sid == "PassTaskRolesToEcsOnly"]) == "ecs-tasks.amazonaws.com"
    error_message = "iam:PassRole must be limited to ECS tasks."
  }

  assert {
    condition     = length(one([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Resource if s.Sid == "PassTaskRolesToEcsOnly"])) == 3
    error_message = "iam:PassRole covers the execution role and the two task roles only."
  }

  assert {
    condition     = length(one([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Resource if s.Sid == "EcrPush"])) == 2
    error_message = "ECR push is limited to the two repositories."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      !anytrue([for a in flatten([s.Action]) : endswith(a, ":*")])
    ])
    error_message = "No service-wide wildcard actions."
  }
}

run "existing_oidc_provider_can_be_reused" {
  variables {
    existing_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  }

  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0
    error_message = "No second provider may be created."
  }
}

run "default_deploy_policy_has_no_console_permissions" {
  assert {
    condition     = !anytrue([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : anytrue([for a in flatten([s.Action]) : startswith(a, "cloudfront:") || startswith(a, "s3:")])])
    error_message = "With console_hosting = ecs the deploy role gets no S3 or CloudFront permissions."
  }
}

run "console_on_cloudfront_adds_only_upload_and_invalidate" {
  variables {
    console_hosting = "s3_cloudfront"
  }

  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Action if s.Sid == "ConsoleUpload"])) == toset(["s3:PutObject", "s3:DeleteObject"]) &&
      one([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Resource if s.Sid == "ConsoleUpload"]) == "arn:aws:s3:::forgeeval-prod-console-${data.aws_caller_identity.current.account_id}/*" &&
      one([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Resource if s.Sid == "ConsoleBucketList"]) == "arn:aws:s3:::forgeeval-prod-console-${data.aws_caller_identity.current.account_id}"
    )
    error_message = "Upload is limited to the console bucket and to put and delete."
  }

  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Action if s.Sid == "ConsoleInvalidate"])) == toset(["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]) &&
      one([for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement : s.Resource if s.Sid == "ConsoleInvalidate"]) == "arn:aws:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/*"
    )
    error_message = "CloudFront access is invalidation only, within this account."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      s.Resource != "*" || contains(["EcrLogin", "EcsTaskDefinitions", "FindLoadBalancer"], s.Sid)
    ])
    error_message = "Still no Resource * beyond the three APIs that lack resource-level permissions."
  }

  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:hxyng/forgeeval:environment:production"
    error_message = "The deploy role trust is unchanged: the production environment only."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.deploy.policy).Statement :
      !anytrue([for a in flatten([s.Action]) : endswith(a, ":*")])
    ])
    error_message = "No service-wide wildcard actions."
  }
}

run "invalid_console_hosting_is_rejected" {
  command = plan

  variables {
    console_hosting = "cdn"
  }

  expect_failures = [var.console_hosting]
}
