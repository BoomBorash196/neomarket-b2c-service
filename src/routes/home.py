"""Home page routes (banners and collections)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List

from src.database import get_db
from src.models import BannerModel, CollectionModel
from src.schemas import Banner, Collection

router = APIRouter()


@router.get("/banners", response_model=List[Banner])
async def get_active_banners(db: AsyncSession = Depends(get_db)):
    """Get active promotional banners."""
    result = await db.execute(
        select(BannerModel)
        .where(BannerModel.is_active == True)
        .order_by(BannerModel.priority.desc())
    )
    banners = result.scalars().all()

    return [
        Banner(
            banner_id=b.banner_id,
            title=b.title,
            image_url=b.image_url,
            link_url=b.link_url,
            priority=b.priority,
            is_active=b.is_active
        )
        for b in banners
    ]


@router.get("/collections", response_model=List[Collection])
async def get_collections(db: AsyncSession = Depends(get_db)):
    """Get active product collections."""
    result = await db.execute(
        select(CollectionModel)
        .where(CollectionModel.is_active == True)
    )
    collections = result.scalars().all()

    # In real implementation, fetch product details from B2B
    return [
        Collection(
            collection_id=c.collection_id,
            title=c.title,
            description=c.description,
            products=[],  # Will be populated from B2B
            max_display=c.max_display
        )
        for c in collections
    ]


@router.get("", response_model=dict)
async def get_home_data(db: AsyncSession = Depends(get_db)):
    """Get all data needed for home page in one call."""
    banners = await get_active_banners(db)
    collections = await get_collections(db)

    return {
        "banners": banners,
        "collections": collections,
        "featured_categories": []  # Will be populated from B2B
    }
