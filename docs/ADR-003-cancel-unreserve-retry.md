# ADR-003: Async Retry for Failed Cancel Unreserve

## Context

When a buyer cancels an order, B2C calls the B2B `POST /inventory/unreserve`
endpoint to release the stock reservation. If B2B is unavailable (5xx), the
buyer must still get an immediate `CANCEL_PENDING` answer — the cancellation
intent cannot be lost. The order is persisted as `CANCEL_PENDING` *before* the
B2B call, and a background worker retries the release until it succeeds.

## Options Considered

### Option 1: Celery task with exponential backoff
Decouple unreserve into a Celery task, enqueued on every cancel attempt, with
`autoretry_for`, exponential backoff and a dead-letter path.

**Pros:** built-in retry/backoff, worker isolation, industry standard.

**Cons:** requires Redis/RabbitMQ plus a separate Celery worker deployment;
more moving parts to configure and monitor.

### Option 2: Management command (cron) with DB-persisted backoff (chosen)
A periodic CLI command scans `orders` for due `CANCEL_PENDING` rows and calls
B2B unreserve.

**Pros:** zero new infrastructure; runs with the existing service deployment;
retry state (`cancel_retry_count`, `cancel_next_retry_at`, `cancel_last_error`)
lives in the database, so a service restart cannot lose pending work.

**Cons:** cron must be configured; no dead-letter queue (the last error and
attempt count are stored on the order for manual review).

### Option 3: Django Q
Django-native ORM task queue with retry support.

**Pros:** task state is stored in the DB, no external broker.

**Cons:** the project is FastAPI + SQLAlchemy, not Django; adding Django just
for the queue is a stack mismatch.

## Decision

**Chose Option 2: management command by cron.**

**Criteria:**
1. **Simplicity of setup** — no broker or extra worker process; one cron line
   and the existing database.
2. **Guarantee of execution on restart** — the intent and the next retry
   timestamp are committed to `orders` before the B2B call. After a restart,
   the next cron run picks up every due order; Celery would additionally need
   a durable broker and worker supervision.

## Implementation

- `POST /api/v1/orders/{id}/cancel`: validates the status, persists
  `CANCEL_PENDING`, then calls B2B `POST /inventory/unreserve`. Success →
  `CANCELLED`; failure → stays `CANCEL_PENDING`, increments
  `cancel_retry_count`, stores `cancel_last_error`, and sets
  `cancel_next_retry_at` with exponential backoff (1 min → 2 → 4 … capped at
  1 hour).
- `POST /api/v1/orders/{id}/cancel-retry`: manual/worker trigger for one
  order.
- `python -m src.workers.retry_cancel_pending --limit 100`: cron entrypoint
  that processes all due `CANCEL_PENDING` orders. B2B unreserve is idempotent
  by `order_id`, so concurrent runs are safe.
- Allowed cancellation statuses are `CREATED` and `PAID`; anything else
  returns `409 CANCEL_NOT_ALLOWED` with `current_status`.
