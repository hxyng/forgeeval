# ADR 0001: Job queue — Redis Streams locally, Amazon SQS in AWS

- Status: accepted
- Date: 2026-10-12

## Context

An evaluation run fans out into one job per dataset item. Jobs call external models, so they are
slow and fail in ordinary ways: timeouts, rate limits, worker crashes. We need delivery that
survives a dead worker, retries with backoff, a place for jobs that keep failing, and a local
setup that starts with `docker compose up`.

## Decision

1. **The queue is a wake-up signal, not the source of truth.** Each message carries only a
   `run_items.id`. A worker claims the item with a conditional `UPDATE … WHERE status IN
   ('queued','retry_wait')`, so a duplicate delivery loses the race and does nothing. Final
   writes are fenced on `leased_by`. Retry timing, attempt counts and dead-lettering live in
   Postgres (`run_items`, `dead_letters`), and a reaper re-enqueues anything the queue lost.
2. **One `JobQueue` protocol, two implementations** selected by `QUEUE_BACKEND`:
   - `redis`: Redis Streams with a consumer group; `XAUTOCLAIM` redelivers messages a crashed
     consumer never acked after a visibility timeout.
   - `sqs`: Amazon SQS standard queue with long polling, a 120 s visibility timeout and a
     redrive policy to `eval-jobs-dlq` after 5 receives.
3. Trace context (`traceparent`) travels in stream fields or SQS message attributes, so one
   trace spans API → queue → worker.

## Consequences

- Both backends are **at-least-once** and may reorder. That is acceptable only because claims
  are idempotent; any future consumer must keep that property.
- SQS's DLQ and our `dead_letters` table overlap. The table is authoritative (it knows the error
  class and attempts); the SQS DLQ only catches messages whose worker died before acking five
  times, and an alarm on its depth flags a crash loop.
- Redis needs nothing beyond the compose service; SQS removes a stateful component in AWS and
  scales the worker service on `ApproximateNumberOfMessagesVisible`.
- Local SQS parity: `docker compose -f docker-compose.yml -f docker-compose.sqs.yml up` runs the
  same code against ElasticMQ with the same DLQ policy (`infra/local/elasticmq.conf`).

## Alternatives considered

- **Celery / RQ**: adds a framework whose retry and result semantics would duplicate what the
  database already tracks, and hides delivery behaviour we want to explain and test.
- **Postgres as the queue (`SELECT … FOR UPDATE SKIP LOCKED`)**: one fewer component and fully
  transactional; rejected for now because polling load lands on the primary database. Worth
  revisiting if the queue becomes the only reason to run Redis.
- **SQS FIFO**: exactly-once processing within 5 minutes, but 300 msg/s per group limits and we
  don't need ordering; idempotent claims already give the effect we need.
