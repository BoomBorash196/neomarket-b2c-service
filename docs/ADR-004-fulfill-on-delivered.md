# ADR-004: Trigger Fulfill on DELIVERED

## Context

When an order reaches DELIVERED, B2C must call B2B `POST /inventory/fulfill` to finalize
reserved stock. If fulfill fails, the order must stay DELIVERED (goods are with the buyer)
and retry asynchronously.

## Options Considered

### Option 1: Django `post_save` signal
Fire fulfill when `Order.status` changes to DELIVERED inside a model signal.

**Pros:** Automatic on any save path.  
**Cons:** Stack is FastAPI + SQLAlchemy, not Django; signals are hard to test in isolation;
risk of double-fire on unrelated saves.

### Option 2: Django Admin action
Operator marks order delivered in Admin UI, action calls fulfill.

**Pros:** Explicit human trigger.  
**Cons:** No Django Admin in this service; not callable from logistics API or tests without Admin.

### Option 3: Explicit service function on status-update endpoint
`PATCH /api/v1/orders/{id}/status` (internal, `X-Service-Key`) transitions status;
when new status is DELIVERED, `on_order_delivered()` calls B2B fulfill. On failure,
`BackgroundTasks` schedules `retry_fulfill_order`; `POST /fulfill-retry` for manual/cron retry.
`fulfill_completed` flag on `orders` provides client-side idempotency.

**Pros:** Testable without Admin; clear transition boundary; matches B2C-13 sequence.  
**Cons:** Every DELIVERED path must go through the endpoint (acceptable for logistics integration).

## Decision

**Chose Option 3: explicit status-update endpoint + fulfill service.**

**Criteria:**
1. **Risk of accidental double fulfill** — transition detected once; `fulfill_completed` skips repeat B2B calls; B2B idempotent by `order_id`.
2. **Testability without Django Admin** — pytest hits `PATCH .../status` with service key and mocks B2B.

## Implementation

- `src/services/order_fulfill.py` — `on_order_delivered`, `retry_fulfill_order`
- `PATCH /api/v1/orders/{order_id}/status` — internal status update, triggers fulfill on DELIVERED
- `POST /api/v1/orders/{order_id}/fulfill-retry` — retry for DELIVERED + `fulfill_completed=false`
- Migration `007_add_fulfill_completed` — `orders.fulfill_completed` boolean
