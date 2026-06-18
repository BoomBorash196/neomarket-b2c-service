# ADR-003: Async Retry for Failed Cancel Unreserve

## Context

When a customer cancels an order, we call B2B `reserve_stock` with negative quantities to release stock reservations. If B2B is unavailable (5xx), we cannot release stock synchronously. The order must not block — customer should see immediate feedback, but stock must eventually be released.

## Options Considered

### Option 1: Celery Task with Exponential Backoff
Decouple unreserve into a Celery task. On cancel failure, enqueue a task with `retry=True`, exponential backoff (`on_failure` logs for manual review).

**Pros:**
- Built-in retry, backoff, dead-letter queue
- Industry standard for Python/FastAPI

**Cons:**
- Requires Redis/RabbitMQ infrastructure
- Adds deployment complexity (Celery worker process)
- Overkill for current scale (single service, no queue infrastructure)

### Option 2: Management Command (Cron / Periodic)
Run a periodic management command (every 5 min) that scans `CANCEL_PENDING` orders and retries unreserve.

**Pros:**
- Zero infrastructure — uses existing service deployment
- Simple to implement: one CLI command + cron entry
- Easy to reason about: every pending order gets retried on each run

**Cons:**
- No per-order retry delay — all pending orders retried at fixed interval
- No dead-letter queue for repeated failures (needs manual age-based fallback)

### Option 3: Django Q
Similar to Celery but Django-native.

**Pros:**
- ORM-based task storage, no external broker needed

**Cons:**
- Project is FastAPI + SQLAlchemy, not Django
- Adding Django dependency is a mismatch with the stack

## Decision

**Chose Option 2: Management Command.**

**Criteria:**
1. **Simplicity of setup** — zero new infrastructure, works with existing deployment.
2. **Guarantee of execution on restart** — cron runs regardless of service state; if the service is up, the command runs within the interval.

**Implementation:**
- `POST /cancel` on B2B failure sets order status to `CANCEL_PENDING` (no exception to user).
- `POST /cancel-retry` endpoint for manual/background retry.
- A management command (`python -m src.commands.retry_cancel_pending`) scans for `CANCEL_PENDING` orders older than N minutes and retries unreserve.
- Scaffold in v1: log the error + leave order in `CANCEL_PENDING` without automatic retry. The `cancel-retry` endpoint exists for manual invocation.
