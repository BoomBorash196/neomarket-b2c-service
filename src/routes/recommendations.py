"""Recommendation routes."""

from fastapi import APIRouter, HTTPException, status
from typing import List

from src.schemas import RecommendationList, ProductBasic
from src.services.b2b_client import b2b_client, B2BClientError

router = APIRouter()


@router.get("/products/{product_id}", response_model=RecommendationList)
async def get_recommendations(product_id: str, limit: int = 4):
    """Get recommended products for the given product."""
    try:
        current_product = await b2b_client.get_product_by_id(product_id)
    except B2BClientError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "B2B_ERROR", "message": f"B2B error: {exc.message}"},
        ) from exc

    if not current_product:
        return RecommendationList(
            current_product_id=product_id,
            recommendations=[],
            reason="product_not_found"
        )

    category_id = current_product.get("category_id", "")
    parent_category_id = current_product.get("parent_category_id")

    recommendations: List[ProductBasic] = []
    from_same_category = False
    from_parent = False
    seen_ids: set = set()

    # Get products from same category
    if category_id:
        try:
            products = await b2b_client.get_products_by_category(
                category_id=category_id,
                page=1,
                page_size=limit * 2
            )
        except B2BClientError:
            products = {"products": []}
        for p in products.get("products", []):
            pid = p.get("product_id", "")
            if pid and pid != product_id and pid not in seen_ids and len(recommendations) < limit:
                recommendations.append(ProductBasic(
                    id=pid,
                    name=p.get("title", ""),
                    main_image_url=p.get("main_image_url", ""),
                    min_price=float(p.get("min_price", 0.0)),
                    has_stock=bool(p.get("is_available", True))
                ))
                seen_ids.add(pid)
                from_same_category = True

    # Fill from parent category if needed
    if len(recommendations) < limit and parent_category_id:
        try:
            more_products = await b2b_client.get_products_by_category(
                category_id=parent_category_id,
                page=1,
                page_size=(limit - len(recommendations)) * 2
            )
        except B2BClientError:
            more_products = {"products": []}
        for p in more_products.get("products", []):
            pid = p.get("product_id", "")
            if pid and pid != product_id and pid not in seen_ids and len(recommendations) < limit:
                recommendations.append(ProductBasic(
                    id=pid,
                    name=p.get("title", ""),
                    main_image_url=p.get("main_image_url", ""),
                    min_price=float(p.get("min_price", 0.0)),
                    has_stock=bool(p.get("is_available", True))
                ))
                seen_ids.add(pid)
                from_parent = True

    # Determine reason
    if from_same_category and len(recommendations) == limit:
        reason = "same_category"
    elif from_parent:
        reason = "parent_category"
    else:
        reason = "no_similar_products"

    return RecommendationList(
        current_product_id=product_id,
        recommendations=recommendations,
        reason=reason
    )
