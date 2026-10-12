# ADR 0002: PostgreSQL as the system of record, JSONB only where shape varies

- Status: accepted
- Date: 2026-10-12

## Context

ForgeEval stores tenants and keys, production traces, versioned datasets / prompts / evaluators,
runs fanned out into items, scores, and gate verdicts. Comparisons join two runs item by item;
gates aggregate scores with percentiles; idempotent ingest needs a uniqueness guarantee; and
versions must be immutable once a run has used them.

## Decision

- **PostgreSQL 16** (RDS in AWS) with a normalized schema: 21 tables in five domains, managed by
  Alembic migrations, with a test that fails if models and migrations drift.
- **Typed columns for anything filtered, joined or aggregated** (latency, cost, status, tokens,
  timestamps, scores). **JSONB only where the shape is the user's**: model inputs/outputs, span
  attributes, evaluator configs, expected outputs.
- **Integrity in the database, not just the app**: `UNIQUE (project_id, idempotency_key)` makes
  trace ingest idempotent with `ON CONFLICT DO NOTHING`; `UNIQUE (run_item_id,
  evaluator_version_id)` makes score writes idempotent; a trigger rejects edits to items of a
  frozen dataset version.
- **UUIDv7 primary keys**: sortable by creation time, so keyset pagination works on the id and
  index locality is good.
- Statistics (`percentile_cont`, paired comparisons) run in SQL and Python over the same rows.
- Large trace payloads (> 64 KiB) are offloaded to S3; the row keeps `payload_s3_key`.

## Consequences

- Version immutability and idempotency hold even for writes that bypass the API.
- Trace volume is the scaling risk: at ~10× we partition `traces`/`spans` by month and drop old
  partitions for retention, and move span analytics to a columnar store if dashboards start
  competing with OLTP writes.
- Single-writer RDS is enough for the MVP; Multi-AZ is a Terraform variable.

## Alternatives considered

- **Document store (MongoDB/DynamoDB)**: flexible payloads, but run-vs-baseline joins, percentile
  aggregates and cross-entity constraints become application code.
- **ClickHouse for everything**: excellent for trace analytics, poor fit for transactional state
  (claims, leases, versioning). Kept as the 10× option for spans only.
