# ForgeEval

An open-source platform that captures LLM application traces, replays them against new prompt or
model versions, scores quality and safety, and blocks regressions before deployment.

> Scope rule: this is not a chatbot. It is the machinery that proves whether a chatbot, or any LLM
> feature, is getting better.

Design: [docs/data-model.html](docs/data-model.html) has the ERD, table reference, infrastructure
diagram and job lifecycle.

## Run it locally

Requires Docker Desktop. From a clean checkout:

```bash
cp .env.example .env
docker compose up -d --build --wait
curl localhost:8000/readyz
```

That starts Postgres (host port **5433**, so it won't clash with a native install), Redis, a one-shot
migration job, the API on `localhost:8000` (OpenAPI docs at `/docs`), two worker replicas, an
OpenTelemetry Collector and Jaeger. Traces are at **http://localhost:16686**: one trace follows an
evaluation from the HTTP request through the Redis queue to every worker span and Postgres query.

Create a project and an API key (printed once, store it):

```bash
docker compose exec api python -m forgeeval.admin bootstrap --org acme --project support-bot
curl -H "Authorization: Bearer fe_..." localhost:8000/v1/whoami
```

## Queue and object storage

Locally the job queue is Redis Streams; in AWS it is SQS with a dead-letter queue. To run the
same code on SQS semantics locally (ElasticMQ, same redrive policy as Terraform):

```bash
docker compose -f docker-compose.yml -f docker-compose.sqs.yml up -d --build --wait
```

Trace payloads over 64 KiB go to S3 (S3Mock locally on port 9091), which raises the per-trace
limit from 256 KiB to 2 MiB. Why the queue is only a wake-up signal and the database is the
source of truth: [ADR 0001](docs/adr/0001-job-queue.md); schema choices:
[ADR 0002](docs/adr/0002-database.md).

## Model-call cache

The worker memoises successful model calls in Postgres (`model_call_cache`), for both generations and LLM-judge verdicts, keyed by a hash of kind, model, params and prompt (plus `expected` for `fake/oracle`). Re-running an unchanged prompt, dataset and evaluators makes no model calls and adds $0 to the run's `cost_usd`; cached answers keep their original latency, tokens and cost, so latency and cost evaluators score them the same. `GET /v1/runs/{id}` reports `cache_hits`, and `POST /v1/runs` accepts `use_cache: false` to force fresh calls. Calls with `temperature > 0` are never cached unless `CACHE_SAMPLED_OUTPUTS=true`, entries older than `CACHE_MAX_AGE_DAYS` (30) are ignored, and `CACHE_ENABLED=false` turns the cache off. Judge prompts put the shared rubric first and the per-item fields last so provider-side prefix caching can apply.

## Dashboards

The collector turns the API's and workers' OTLP metrics into Prometheus metrics (**http://localhost:9090**),
and Grafana (**http://localhost:3001**, anonymous viewer access for local use) provisions the
"ForgeEval operations" dashboard from `observability/grafana/`: request latency p50/p95 by route, HTTP
error rate, queue depth, worker throughput by outcome, item processing time, evaluation cost, runs
created and ingested traces. The dashboard links to Jaeger for the matching traces.

## Develop the backend

```bash
cd backend
uv sync
uv run pytest        # needs the compose Postgres running
uv run ruff check . && uv run mypy forgeeval
```

Schema changes: edit `forgeeval/db/models.py`, then
`uv run alembic revision --autogenerate -m "<what changed>"` and review the generated file.
`tests/test_schema.py::test_models_match_migration` fails if models and migrations drift.

## CI regression gate

`forgeeval gate` (in [`sdk/`](sdk/README.md)) runs an evaluation for a prompt change and fails the
build when a regression gate fails. The composite action
[`.github/actions/forgeeval-gate`](.github/actions/forgeeval-gate/action.yml) wraps it for GitHub
Actions and writes a Markdown summary (gates, paired deltas with 95% CIs, newly failing items) to
the job summary.

[`.github/workflows/eval-gate.yml`](.github/workflows/eval-gate.yml) is a runnable demo: it starts
the docker-compose stack, seeds [`examples/triage`](examples/triage) (120 tickets, an accuracy gate
and a p95 latency gate), expects `fake/oracle` to pass and `fake/echo` to be blocked.

## Web console

[`web/`](web/) is a React + TypeScript console for reading evaluation results: sign in with a project
API key, list runs, open a run to see its metrics and the exact cases that failed (input, expected,
actual output, evaluator rationale), compare a run with its baseline (paired deltas with 95%
confidence intervals), review regression-gate verdicts, browse production traces and start a run.

```bash
docker compose up -d --build --wait     # includes the console on http://localhost:3000
```

Paste an API key (see above) on the sign-in page. The key stays in `sessionStorage` for that tab only.

Develop it with the Vite dev server, which proxies `/v1` to the API on `localhost:8000`:

```bash
cd web
pnpm install
pnpm dev            # http://localhost:5173
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm e2e            # Playwright; needs FORGEEVAL_API_KEY, a seeded stack and a regressed run
```

The API client is typed from [`docs/openapi.json`](docs/openapi.json). After the API contract changes,
run `pnpm gen:api` and commit `web/src/api/schema.d.ts`; CI fails when it is stale (`pnpm check:api`).

## Deploy to AWS

The production environment (ECS Fargate, ALB, RDS PostgreSQL, SQS, S3, X-Ray and CloudWatch) is
described in Terraform under [`infra/`](infra/README.md): architecture, bootstrap and apply steps, how to
set the model API key, a monthly cost estimate and teardown. The checks (`fmt`, `validate`, `tflint`,
Trivy, `terraform test` with a mocked provider) run without an AWS account.

- `.github/workflows/infra.yml` runs those checks on every pull request that touches `infra/`, plus a
  read-only `terraform plan` once the repository variable `AWS_PLAN_ROLE_ARN` is set.
- `.github/workflows/deploy.yml` builds and pushes both images, runs the migration task, rolls the ECS
  services and smoke-tests `/readyz`. It authenticates with GitHub OIDC and skips itself until the
  repository variable `AWS_DEPLOY_ROLE_ARN` is set.

First-time setup, routine deploys, rollback, failed migrations and dead-letter messages are in
[`docs/runbooks/deploy-and-rollback.md`](docs/runbooks/deploy-and-rollback.md).

## Layout

| Path | What lives there |
|---|---|
| `backend/forgeeval/api` | FastAPI service: auth, ingest, runs, gates |
| `backend/forgeeval/worker` | Evaluation workers: queue consumer, evaluators |
| `backend/forgeeval/db` | SQLAlchemy models and session |
| `backend/migrations` | Alembic migrations |
| `sdk/` | Python client SDK: trace capture, local PII redaction, batching, retries |
| `web/` | React + TypeScript web console: runs, failing cases, compare, gates, traces |
| `infra/` | Terraform for the AWS production environment (`bootstrap`, `envs/prod`, `modules/`); `infra/local` holds emulator config |
| `docs/adr` | Architecture decision records |
