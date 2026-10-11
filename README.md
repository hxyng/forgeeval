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
migration job, the API on `localhost:8000` (OpenAPI docs at `/docs`) and two worker replicas.

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

## Layout

| Path | What lives there |
|---|---|
| `backend/forgeeval/api` | FastAPI service: auth, ingest, runs, gates |
| `backend/forgeeval/worker` | Evaluation workers: queue consumer, evaluators |
| `backend/forgeeval/db` | SQLAlchemy models and session |
| `backend/migrations` | Alembic migrations |
| `sdk/` | Python client SDK: trace capture, redaction, batching (next layer) |
| `web/` | React + TypeScript console (later layer) |
| `infra/` | Terraform for AWS: ECS, RDS, SQS, S3 (later layer) |
