"""Home page routes (banners and collections)."""

import json
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from typing import List, Optional
from datetime import datetime, timezone

from src.database import get_db
from src.models import BannerModel, CollectionModel
from src.schemas import Banner, Collection, CollectionProduct
from src.services.b2b_client import b2b_client, B2BClientError

router = APIRouter()


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------

@router.get("/banners", response_model=List[Banner])
async def get_active_banners(
    db: AsyncSession = Depends(get_db),
    now: Optional[datetime] = Query(None, description="Current time for testing (ISO 8601)"),
):
    """Get active promotional banners visible at the given time."""
    current = now or datetime.now(timezone.utc)

    conditions = [
        BannerModel.is_active == True,
    ]

    result = await db.execute(
        select(BannerModel)
        .where(and_(*conditions))
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
            is_active=b.is_active,
        )
        for b in banners
    ]


# ---------------------------------------------------------------------------
# Collections — list (metadata only, no products)
# ---------------------------------------------------------------------------

@router.get("/collections", response_model=List[Collection])
async def get_collections(db: AsyncSession = Depends(get_db)):
    """Get active product collections — metadata only, no product details."""
    result = await db.execute(
        select(CollectionModel)
        .where(CollectionModel.is_active == True)
    )
    collections = result.scalars().all()

    return [
        Collection(
            collection_id=c.collection_id,
            title=c.title,
            description=c.description,
            products=[],
            max_display=c.max_display,
            unavailable_ids=[],
        )
        for c in collections
    ]


# ---------------------------------------------------------------------------
# Collections — single collection with B2B enrichment
# ---------------------------------------------------------------------------

@router.get("/collections/{collection_id}", response_model=Collection)
async def get_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a single collection with product details enriched from B2B.

    B2C stores only product UUIDs (product_ids). Product details are
    fetched in batch from B2B. Products that are not found / inactive
    / out of stock in B2B are collected into ``unavailable_ids`` and
    excluded from ``items``.

    Returns 404 if the collection does not exist.
    """
    # 1. Fetch collection from DB
    result = await db.execute(
        select(CollectionModel).where(CollectionModel.collection_id == collection_id)
    )
    collection = result.scalar_one_or_none()

    if collection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "COLLECTION_NOT_FOUND",
                "message": f"Collection {collection_id} not found",
            },
        )

    # 2. Parse stored product UUIDs (JSON-encoded in Text column)
    product_ids_raw = collection.product_ids or "[]"
    try:
        product_ids: List[str] = json.loads(product_ids_raw)
    except json.JSONDecodeError:
        product_ids = []

    # 3. Batch-enrich from B2B
    items: List[CollectionProduct] = []
    unavailable_ids: List[str] = []

    if product_ids:
        try:
            b2b_products = await b2b_client.get_products_by_ids(product_ids)
        except B2BClientError:
            b2b_products = {}

        for pid in product_ids:
            product_data = b2b_products.get(str(pid))
            if product_data is None:
                unavailable_ids.append(pid)
                continue

            is_available = product_data.get("is_active", True) and product_data.get("is_available", True)
            if not is_available:
                unavailable_ids.append(pid)
                continue

            items.append(
                CollectionProduct(
                    product_id=str(pid),
                    title=product_data.get("title", ""),
                    main_image_url=product_data.get("main_image_url", ""),
                    min_price=float(product_data.get("min_price", 0.0)),
                )
            )

            if len(items) >= collection.max_display:
                break

    # 4. All products unavailable → items=[] is valid, not an error
    return Collection(
        collection_id=collection.collection_id,
        title=collection.title,
        description=collection.description,
        products=items,
        max_display=collection.max_display,
        unavailable_ids=unavailable_ids,
    )


# ---------------------------------------------------------------------------
# Home data — banners + collections in one call
# ---------------------------------------------------------------------------

@router.get("", response_model=dict)
async def get_home_data(db: AsyncSession = Depends(get_db)):
    """Get all data needed for home page in one call."""
    banners = await get_active_banners(db)
    collections = await get_collections(db)

    return {
        "banners": banners,
        "collections": collections,
        "featured_categories": []
    }