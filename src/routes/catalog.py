"""Catalog routes."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
from pydantic import BaseModel

from src.database import get_db
from src.schemas import ProductDetail, CategoryNode, FilterOption, ProductFilters
from src.services.b2b_client import b2b_client

router = APIRouter()


class ProductListResponse(BaseModel):
    """Paginated product list."""
    products: List[ProductDetail]
    total: int
    page: int
    page_size: int
    filters_applied: Optional[dict] = None


@router.get("/categories", response_model=List[dict])
async def get_categories():
    """Get category tree from B2B."""
    categories = await b2b_client.get_categories()
    return categories


@router.get("/products", response_model=ProductListResponse)
async def get_products(
    category_id: Optional[str] = Query(None, description="Filter by category"),
    search: Optional[str] = Query(None, description="Search by product name"),
    min_price: Optional[float] = Query(None, description="Minimum price"),
    max_price: Optional[float] = Query(None, description="Maximum price"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    db: AsyncSession = Depends(get_db)
):
    """Get products with optional filtering."""
    filters = {}
    if category_id:
        filters["category_id"] = category_id
    if search:
        filters["search"] = search
    if min_price is not None:
        filters["min_price"] = min_price
    if max_price is not None:
        filters["max_price"] = max_price

    result = await b2b_client.get_products_by_category(
        category_id=category_id or "",
        page=page,
        page_size=page_size,
        filters=filters
    )

    products = []
    for product_data in result.get("products", []):
        # Simplified - in real implementation, fetch full details
        products.append(ProductDetail(
            product_id=product_data.get("product_id", ""),
            title=product_data.get("title", ""),
            main_image_url=product_data.get("main_image_url", ""),
            min_price=product_data.get("min_price", 0.0),
            is_available=product_data.get("is_available", True),
            description=product_data.get("description", ""),
            images=product_data.get("images", []),
            characteristics=product_data.get("characteristics", {}),
            skus=[]
        ))

    return ProductListResponse(
        products=products,
        total=result.get("total", 0),
        page=page,
        page_size=page_size,
        filters_applied=filters if filters else None
    )


@router.get("/products/{product_id}", response_model=ProductDetail)
async def get_product(product_id: str, db: AsyncSession = Depends(get_db)):
    """Get full product details."""
    product_data = await b2b_client.get_product_by_id(product_id)
    
    if not product_data:
        raise HTTPException(
            status_code=404,
            detail="Product not found"
        )

    return ProductDetail(
        product_id=product_data.get("product_id", ""),
        title=product_data.get("title", ""),
        main_image_url=product_data.get("main_image_url", ""),
        min_price=product_data.get("min_price", 0.0),
        is_available=product_data.get("is_available", True),
        description=product_data.get("description", ""),
        images=product_data.get("images", []),
        characteristics=product_data.get("characteristics", {}),
        skus=[]
    )


@router.get("/categories/{category_id}/filters", response_model=ProductFilters)
async def get_category_filters(category_id: str, db: AsyncSession = Depends(get_db)):
    """Get available filters for a category."""
    # In real implementation, fetch from B2B or cache
    return ProductFilters(
        category_id=category_id,
        filters=[
            FilterOption(
                name="brand",
                label="Brand",
                values=[
                    {"value": "apple", "label": "Apple", "count": 45},
                    {"value": "samsung", "label": "Samsung", "count": 67},
                    {"value": "xiaomi", "label": "Xiaomi", "count": 34}
                ]
            ),
            FilterOption(
                name="price_range",
                label="Price",
                values=[
                    {"value": "0-50000", "label": "Up to 50,000", "count": 120},
                    {"value": "50000-100000", "label": "50,000 - 100,000", "count": 85},
                    {"value": "100000-", "label": "Over 100,000", "count": 32}
                ]
            )
        ]
    )
