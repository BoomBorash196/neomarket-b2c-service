"""API routes."""

from fastapi import APIRouter

from . import cart, order, wishlist, catalog, home, recommendations

router = APIRouter()
router.include_router(cart.router)
router.include_router(order.router)
router.include_router(wishlist.router)
router.include_router(catalog.router)
router.include_router(home.router)
router.include_router(recommendations.router)
