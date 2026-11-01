"""Cron entrypoint: retry CANCEL_PENDING order unreserves.

Run every minute, for example::

    * * * * * cd /app && python -m src.workers.retry_cancel_pending --limit 100

The worker is safe to run concurrently: B2B unreserve is idempotent by
``order_id`` and each order moves to CANCELLED only after a successful
response. Exponential backoff is persisted in ``orders.cancel_next_retry_at``.
"""

import argparse
import asyncio
import logging

from src.database import async_session_maker
from src.services.order_cancel import process_cancel_retries

logger = logging.getLogger(__name__)


async def run(limit: int = 100) -> int:
    async with async_session_maker() as db:
        processed = await process_cancel_retries(db, limit=limit)
    logger.info("cancel retry worker processed %s orders", processed)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum number of orders to process per run",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(limit=args.limit))


if __name__ == "__main__":
    main()
