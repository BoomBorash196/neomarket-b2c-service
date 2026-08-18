"""Banner click events for CTR analytics.

Public endpoint — no authentication required.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.database import get_db
from src.models import BannerModel, BannerClickModel
from src.schemas import BannerClickCreate

router = APIRouter()


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def record_banner_click(
    body: BannerClickCreate,
    db: AsyncSession = Depends(get_db),
):
    """Record a banner click for CTR analytics.

    - Banner must exist → 400 if not found.
    - No authentication required — user_id may be null for guests.
    - Idempotent-safe: duplicate clicks are allowed (clicks are events,
      not subscriptions).
    """
    # Validate banner exists
    result = await db.execute(
        select(BannerModel).where(BannerModel.banner_id == body.banner_id)
    )
    banner = result.scalar_one_or_none()

    if not banner:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BANNER_NOT_FOUND",
                "message": f"Banner {body.banner_id} not found",
            },
        )

    # Record click
    click = BannerClickModel(
        banner_id=body.banner_id,
        user_id=body.user_id,  # may be None for guests
    )
    db.add(click)
    await db.commit()

    return None  # 204
