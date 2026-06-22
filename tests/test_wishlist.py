"""Tests for wishlist/favorites endpoints — US-CART-01.

Covers:
  - add_to_favorites_returns_201              (happy path, first add)
  - repeat_add_returns_200_not_duplicate      (idempotent, no DB duplicate)
  - blocked_product_excluded_from_list        (B2B blocks product → hidden)
  - user_id_from_query_is_ignored             (IDOR protection)
  - get_empty_wishlist                        (empty state)
  - remove_from_wishlist_idempotent           (204 even if absent)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.services.b2b_client import b2b_client


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture(autouse=True)
def reset_b2b_mocks():
    """Restore original B2B methods after each test."""
    from src.services import b2b_client as _mod
    original = {}
    for name in ("get_product_by_id", "get_skus_by_ids", "get_categories",
                 "get_product_search", "get_similar_products"):
        if hasattr(_mod.b2b_client, name):
            original[name] = getattr(_mod.b2b_client, name)
    yield
    for name, val in original.items():
        setattr(_mod.b2b_client, name, val)


# ======================================================================
# Helpers
# ======================================================================

def _make_product(product_id: str, title: str, price: float, available: bool = True):
    return {
        "product_id": product_id,
        "title": title,
        "main_image_url": "http://img",
        "min_price": price,
        "is_available": available,
        "description": "",
        "images": [],
        "characteristics": {},
        "skus": [],
    }


def _auth_headers(user_id: str) -> dict:
    return {"X-User-Id": user_id}


# ======================================================================
# TEST 1 — add_to_favorites_returns_201
# ======================================================================

@pytest.mark.asyncio
async def test_add_to_favorites_returns_201(client: TestClient, db_session):
    """First add → 201 CREATED with product details."""
    from src.models import WishlistItemModel
    from sqlalchemy import select

    product = _make_product("p1", "Wireless Headphones", 4999.0)
    b2b_client.get_product_by_id = AsyncMock(return_value=product)

    resp = client.post(
        "/api/v1/wishlist",
        json={"product_id": "p1"},
        headers=_auth_headers("user-42"),
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["product_id"] == "p1"
    assert data["product_title"] == "Wireless Headphones"
    assert data["min_price"] == 4999.0
    assert data["is_available"] is True
    assert data["wishlist_item_id"] is not None

    # Verify exactly 1 row in DB
    result = await db_session.execute(
        select(WishlistItemModel).where(
            (WishlistItemModel.user_id == "user-42") &
            (WishlistItemModel.product_id == "p1")
        )
    )
    assert len(result.scalars().all()) == 1


# ======================================================================
# TEST 2 — repeat_add_returns_200_not_duplicate
# ======================================================================

@pytest.mark.asyncio
async def test_repeat_add_returns_200_not_duplicate(client: TestClient, db_session):
    """Second add of same product → 200, no DB duplicate.

    Direct DB insert (like test_cart.py) to avoid aiosqlite session isolation.
    """
    from src.models import WishlistItemModel
    from sqlalchemy import select, and_

    product = _make_product("p1", "Wireless Headphones", 4999.0)
    b2b_client.get_product_by_id = AsyncMock(return_value=product)

    # Insert item directly into DB (first add)
    new_item = WishlistItemModel(user_id="user-42", product_id="p1")
    db_session.add(new_item)
    await db_session.commit()
    await db_session.refresh(new_item)
    first_id = new_item.wishlist_item_id

    # Verify 1 row in DB
    result = await db_session.execute(
        select(WishlistItemModel).where(
            and_(
                WishlistItemModel.user_id == "user-42",
                WishlistItemModel.product_id == "p1",
            )
        )
    )
    assert len(result.scalars().all()) == 1

    # Second add via HTTP → 200 (idempotent, not 201)
    resp2 = client.post(
        "/api/v1/wishlist",
        json={"product_id": "p1"},
        headers=_auth_headers("user-42"),
    )

    assert resp2.status_code == 200
    data = resp2.json()
    assert data["wishlist_item_id"] == first_id
    assert data["product_id"] == "p1"

    # Verify still only 1 row in DB (no duplicate created)
    result = await db_session.execute(
        select(WishlistItemModel).where(
            and_(
                WishlistItemModel.user_id == "user-42",
                WishlistItemModel.product_id == "p1",
            )
        )
    )
    assert len(result.scalars().all()) == 1


# ======================================================================
# TEST 3 — blocked_product_excluded_from_list
# ======================================================================

@pytest.mark.asyncio
async def test_blocked_product_excluded_from_list(client: TestClient, db_session):
    """B2B returns None for blocked product → hidden from GET."""
    from src.models import WishlistItemModel

    product = _make_product("p-active", "Active Product", 500.0)

    # Add item via HTTP
    b2b_client.get_product_by_id = AsyncMock(return_value=product)
    client.post("/api/v1/wishlist", json={"product_id": "p-active"}, headers=_auth_headers("user-block"))

    # Now make B2B return None (simulating blocked)
    b2b_client.get_product_by_id = AsyncMock(return_value=None)
    resp = client.get("/api/v1/wishlist", headers=_auth_headers("user-block"))

    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_blocked_product_excluded_among_others(client: TestClient, db_session):
    """One product blocked, others still shown."""
    user_id = "user-mixed"

    b2b_client.get_product_by_id = AsyncMock(return_value=_make_product("p1", "Good", 100.0))
    client.post("/api/v1/wishlist", json={"product_id": "p1"}, headers=_auth_headers(user_id))

    b2b_client.get_product_by_id = AsyncMock(return_value=_make_product("p2", "Blocked", 200.0))
    client.post("/api/v1/wishlist", json={"product_id": "p2"}, headers=_auth_headers(user_id))

    def side_effect(pid):
        if pid == "p1":
            return _make_product("p1", "Good", 100.0)
        return None

    b2b_client.get_product_by_id = AsyncMock(side_effect=side_effect)
    resp = client.get("/api/v1/wishlist", headers=_auth_headers(user_id))

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["product_id"] == "p1"


# ======================================================================
# TEST 4 — user_id_from_query_is_ignored (IDOR protection)
# ======================================================================

@pytest.mark.asyncio
async def test_user_id_from_query_is_ignored(client: TestClient):
    """Query param user_id is ignored — must use X-User-Id header."""
    resp = client.get("/api/v1/wishlist?user_id=user-42")
    assert resp.status_code == 401
    data = resp.json()
    assert data["code"] == "MISSING_AUTH"


@pytest.mark.asyncio
async def test_user_id_from_query_ignored_with_header(client: TestClient, db_session):
    """Header wins over query param."""
    product = _make_product("p1", "Headphones", 4999.0)
    b2b_client.get_product_by_id = AsyncMock(return_value=product)

    resp = client.post(
        "/api/v1/wishlist",
        json={"product_id": "p1"},
        params={"user_id": "user-malicious"},
        headers=_auth_headers("user-42"),
    )

    assert resp.status_code == 201

    b2b_client.get_product_by_id = AsyncMock(return_value=product)
    resp2 = client.get("/api/v1/wishlist", headers=_auth_headers("user-42"))
    assert resp2.status_code == 200
    assert len(resp2.json()["items"]) == 1

    b2b_client.get_product_by_id = AsyncMock(return_value=None)
    resp3 = client.get("/api/v1/wishlist", headers=_auth_headers("user-malicious"))
    assert resp3.status_code == 200
    assert resp3.json()["items"] == []


# ======================================================================
# TEST 5 — get_empty_wishlist
# ======================================================================

@pytest.mark.asyncio
async def test_get_empty_wishlist(client: TestClient):
    """New user → empty list."""
    b2b_client.get_product_by_id = AsyncMock(return_value=None)
    resp = client.get("/api/v1/wishlist", headers=_auth_headers("user-new"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["user_id"] == "user-new"


# ======================================================================
# TEST 6 — remove_from_wishlist_idempotent
# ======================================================================

@pytest.mark.asyncio
async def test_remove_from_wishlist_idempotent(client: TestClient, db_session):
    """Delete returns 204; deleting again → 204."""
    product = _make_product("p-del", "Delete Me", 300.0)
    user_id = "user-del"

    b2b_client.get_product_by_id = AsyncMock(return_value=product)
    client.post("/api/v1/wishlist", json={"product_id": "p-del"}, headers=_auth_headers(user_id))

    resp1 = client.delete(f"/api/v1/wishlist/p-del", headers=_auth_headers(user_id))
    assert resp1.status_code == 204

    resp2 = client.delete(f"/api/v1/wishlist/p-del", headers=_auth_headers(user_id))
    assert resp2.status_code == 204


# ======================================================================
# TEST 7 — product_not_found_returns_404
# ======================================================================

@pytest.mark.asyncio
async def test_product_not_found_returns_404(client: TestClient):
    """Adding non-existent B2B product → 404."""
    b2b_client.get_product_by_id = AsyncMock(return_value=None)
    resp = client.post(
        "/api/v1/wishlist",
        json={"product_id": "ghost-product"},
        headers=_auth_headers("user-42"),
    )

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "PRODUCT_NOT_FOUND"


# ======================================================================
# TEST 8 — response schema correctness
# ======================================================================

@pytest.mark.asyncio
async def test_add_to_wishlist_response_schema(client: TestClient):
    """Response matches WishlistItem schema."""
    product = _make_product("p-schema", "Schema Test", 999.0)
    b2b_client.get_product_by_id = AsyncMock(return_value=product)

    resp = client.post(
        "/api/v1/wishlist",
        json={"product_id": "p-schema"},
        headers=_auth_headers("user-schema"),
    )

    assert resp.status_code == 201
    data = resp.json()
    for key in ("wishlist_item_id", "user_id", "product_id", "product_title",
                "main_image_url", "min_price", "is_available", "added_at"):
        assert key in data, f"Missing key: {key}"