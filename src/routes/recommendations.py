"""Recommendation routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
import random

from src.database import get_db
from src.schemas import RecommendationList, ProductBasic
from src.services.b2b_client import b2b_client

router = APIRouter()


@router.get("/products/{product_id}", response_model=RecommendationList)
async def get_recommendations(product_id: str, limit: int = 4, db: AsyncSession = Depends(get_db)):
    """Get recommended products for the given product."""
    # Get current product to find its category
    current_product = await b2b_client.get_product_by_id(product_id)
    
    if not current_product:
        return RecommendationList(
            current_product_id=product_id,
            recommendations=[],
            reason="Product not found"
        )

    category_id = current_product.get("category_id", "")
    
    # Get random products from the same category
    products = await b2b_client.get_products_by_category(
        category_id=category_id,
        limit=limit * 2,  # Get more to filter out current product
        offset=0,
    )

    recommendations = []
    for p in products.get("items", []):
        if p.get("id") != product_id and len(recommendations) < limit:
            recommendations.append(ProductBasic(
                id=p.get("id", ""),
                name=p.get("title", ""),
                main_image_url=p.get("cover_image") or "",
                min_price=p.get("min_price", 0.0),
                has_stock=True
            ))

    # If not enough recommendations, try parent category
    if len(recommendations) < limit:
        parent_category = current_product.get("parent_category_id", "")
        if parent_category:
            more_products = await b2b_client.get_products_by_category(
                category_id=parent_category,
                limit=(limit - len(recommendations)) * 2,
                offset=0,
            )
            
            existing_ids = {p.id for p in recommendations}
            for p in more_products.get("items", []):
                if (p.get("id") != product_id and
                    p.get("id") not in existing_ids and
                    len(recommendations) < limit):
                    recommendations.append(ProductBasic(
                        id=p.get("id", ""),
                        name=p.get("title", ""),
                        main_image_url=p.get("cover_image") or "",
                        min_price=p.get("min_price", 0.0),
                        has_stock=True
                    ))
                    existing_ids.add(p.get("id"))

    return RecommendationList(
        current_product_id=product_id,
        recommendations=recommendations,
        reason="same_category" if len(recommendations) == limit else "parent_category"
    )
