# Runbook: deploy and rollback on AWS

Names below assume the default `name_prefix` of `forgeeval-prod` and region `us-east-1`. Commands need
AWS credentials for the account (your own admin login; CI uses OIDC and never holds keys).

## First-time setup

1. **Bootstrap** (once, local state): creates the Terraform state bucket, the GitHub OIDC provider and
   the two roles.

   ```bash
   cd infra/bootstrap && terraform init && terraform apply
   terraform output
   ```

   If the account already has the GitHub OIDC provider, add `-var existing_oidc_provider_arn=<arn>`.

2. **Apply the environment**:

   ```bash
   cd ../envs/prod
   cp backend.hcl.example backend.hcl        # bucket and region from the bootstrap output
   cp terraform.tfvars.example terraform.tfvars
   terraform init -backend-config=backend.hcl
   terraform apply
   ```

   Set `alarm_email` in `terraform.tfvars` and confirm the subscription email AWS sends. The services
   will not become healthy yet: the ECR repositories are empty. ECS keeps retrying; that is expected.

3. **Set the model API key** (see `infra/README.md`):
   `aws secretsmanager put-secret-value --secret-id forgeeval-prod/model-api-key --secret-string ...`

4. **Create the GitHub environment and variables** (repository settings):
   - Environment `production`. Add required reviewers if you want a manual approval before each deploy.
   - Repository variables:

     | Variable | Value |
     | --- | --- |
     | `AWS_DEPLOY_ROLE_ARN` | `deploy_role_arn` from the bootstrap output. Enables `deploy.yml` |
     | `AWS_PLAN_ROLE_ARN` | `plan_role_arn` from the bootstrap output. Enables the plan job on pull requests |
     | `TF_STATE_BUCKET` | `state_bucket` from the bootstrap output (used by the plan job) |
     | `AWS_REGION` | only if not `us-east-1` |
     | `NAME_PREFIX` | only if not `forgeeval-prod` |
     | `APP_URL` | only with a domain and certificate: the URL the smoke test calls, e.g. `https://eval.example.com` |

   The variables must be set at repository level, because the workflows test them in a job-level
   condition. Until they exist both workflows skip cleanly.

5. **First deploy**: Actions, `deploy`, Run workflow on `main` (a push to `main` does the same). Then open
   `terraform output alb_dns_name` in a browser, or `curl http://<alb dns>/readyz`.

6. **Create the first organisation, project and API key** with `python -m forgeeval.admin bootstrap
   --org <org> --project <project>` (the key is printed once). In AWS run it as a one-off task on the API
   task definition with a command override, then read the key from the task's log stream:

   ```bash
   aws ecs run-task --cluster forgeeval-prod --launch-type FARGATE      --task-definition forgeeval-prod-api      --overrides '{"containerOverrides":[{"name":"api","command":["python","-m","forgeeval.admin","bootstrap","--org","acme","--project","app"]}]}'      --network-configuration "awsvpcConfiguration={subnets=[<task_subnet_ids>],securityGroups=[<task_security_group_id>],assignPublicIp=ENABLED}"
   ```

   The deploy role does not grant this; run it with your own admin credentials. The key lands in the
   `/ecs/forgeeval-prod/api` log group, so delete that log stream afterwards.

## Routine deploy

Merge to `main`. `deploy.yml`:

1. builds `backend` and `web`, pushes them to ECR tagged with the commit SHA (tags are immutable; a
   re-run of the same commit reuses the existing images);
2. registers new revisions of the `migrate`, `api`, `worker` and `web` task definitions (only the
   application image changes; settings are copied from the latest revision);
3. runs the migration task and stops if it exits non-zero, before any service is touched;
4. updates the three services and waits for them to settle;
5. fails if any service ended up on a different revision than the one just deployed (the ECS circuit
   breaker rolled it back);
6. smoke-tests `/readyz` and `/` through the load balancer.

Deploys are serialized (`concurrency: deploy-production`).

Terraform changes to task-definition settings (environment, secrets, CPU and memory) register a new
revision that the services ignore. Run the `deploy` workflow afterwards to roll them out.

## Rollback

The deployment circuit breaker rolls a service back automatically when new tasks cannot start or fail
health checks; the deploy job then fails at step 5. To go back manually, for example after a bad release
that passed its health checks:

```bash
CLUSTER=forgeeval-prod
# 1. See recent revisions and find the last good one
aws ecs list-task-definitions --family-prefix forgeeval-prod-api --sort DESC --max-items 5
# 2. Point each service at the previous revision (repeat for worker and web)
aws ecs update-service --cluster $CLUSTER --service forgeeval-prod-api \
  --task-definition forgeeval-prod-api:<previous revision>
aws ecs wait services-stable --cluster $CLUSTER --services forgeeval-prod-api
```

Or re-run the `deploy` workflow on the previous commit from the Actions tab (choose the old ref): it
reuses that commit's existing images and registers fresh revisions. Rolling back the application does not
roll back the database; see the next section.

## Failed migration

The deploy stops after the migration step and no service has changed, so the running version keeps
serving. The job log prints the task's output and exit code.

1. Read the output in the job log, or in CloudWatch group `/ecs/forgeeval-prod/migrate`.
2. Check the database state with the last known good revision: `alembic current` against the database.
   Migrations run in a transaction on PostgreSQL, so a failed migration normally leaves the schema as
   it was.
3. Fix forward with a new commit (a corrected migration) and deploy again. Do not edit a migration that
   already ran in production.
4. If a migration succeeded but the new application version is bad, roll the application back as above.
   Only roll the schema back (`alembic downgrade`) if the migration has a tested downgrade, by running the
   `forgeeval-prod-migrate` task definition once with a command override
   (`["alembic","downgrade","<revision>"]`), and take an RDS snapshot first.

Running a migration by hand (outputs of `terraform output` in `infra/envs/prod`):

```bash
aws ecs run-task --cluster forgeeval-prod --launch-type FARGATE \
  --task-definition forgeeval-prod-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[<task_subnet_ids>],securityGroups=[<task_security_group_id>],assignPublicIp=ENABLED}"
```

Use `DISABLED` for `assignPublicIp` when `enable_nat_gateway` is true.

## Messages in the dead-letter queue

The alarm `forgeeval-prod-dlq-not-empty` fires when a job message was received five times without being
deleted: the worker crashed or hung on it five times. The `dead_letters` table in Postgres is the
authoritative record of ordinary job failures; DLQ messages mean the worker never finished the job.

1. Look at worker logs in `/ecs/forgeeval-prod/worker` around the alarm time (out-of-memory kills,
   exceptions at startup, a model endpoint hanging past the 120 s visibility timeout).
2. Check whether workers are running and scaling: `aws ecs describe-services` on the worker service, and
   the `ApproximateAgeOfOldestMessage` alarm.
3. Inspect a message without deleting it:

   ```bash
   aws sqs receive-message --queue-url <dlq url> --max-number-of-messages 1 --visibility-timeout 0
   ```

   The body holds only a `run_items.id`; look the item up in the database for its status and error.
4. Fix the cause (code or capacity), deploy, then redrive the messages to the main queue:

   ```bash
   aws sqs start-message-move-task --source-arn <dlq arn> --destination-arn <queue arn>
   ```

   The worker's claim is idempotent, so redriving an item that already finished is harmless. The
   reaper also re-enqueues items whose lease expired, so you can often just let it run.
5. To discard messages for good, purge the DLQ only after confirming the items are marked failed in
   Postgres.

## Database password rotation

RDS rotates the master password in Secrets Manager every 7 days by default. ECS reads secrets only when a
task starts, so a task started before a rotation holds the old password; new database connections from it
can start failing. Until the application re-reads its credentials on authentication failure, redeploy
(or `aws ecs update-service --force-new-deployment` for each service) at least every 7 days, or lengthen
the rotation period of the secret in Secrets Manager. If `/readyz` suddenly fails with authentication
errors, force a new deployment first.
