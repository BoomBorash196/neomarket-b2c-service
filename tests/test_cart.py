"""Tests for cart endpoints — DoD: 4 required pytest cases."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.models import CartItemModel
from src.services.b2b_client import b2b_client


# =====================================================================
# Shared mocks
# =====================================================================

def _mock_sku_ok(
    sku_id: str = "sku-001",
    price: float = 1000.0,
    quantity_available: int = 10,
    is_active: bool = True,
    product_id: str = "prod-001",
    product_title: str = "Test Product",
) -> dict:
    """Return a valid B2B SKU response."""
    return {
        "sku_id": sku_id,
        "product_id": product_id,
        "product_title": product_title,
        "price": price,
        "quantity_available": quantity_available,
        "is_active": is_active,
    }


def _mock_sku_out_of_stock(
    sku_id: str = "sku-002",
    price: float = 500.0,
    quantity_available: int = 0,
    is_active: bool = True,
    product_id: str = "prod-002",
    product_title: str = "Out of Stock Product",
) -> dict:
    return {
        "sku_id": sku_id,
        "product_id": product_id,
        "product_title": product_title,
        "price": price,
        "quantity_available": quantity_available,
        "is_active": is_active,
    }


def _mock_sku_inactive(
    sku_id: str = "sku-003",
    price: float = 300.0,
    quantity_available: int = 5,
    is_active: bool = False,
    product_id: str = "prod-003",
    product_title: str = "Inactive Product",
) -> dict:
    return {
        "sku_id": sku_id,
        "product_id": product_id,
        "product_title": product_title,
        "price": price,
        "quantity_available": quantity_available,
        "is_active": is_active,
    }


# =====================================================================
# 1. add_sku_increments_quantity_if_already_in_cart
# =====================================================================

@pytest.mark.asyncio
async def test_add_sku_increments_quantity_if_already_in_cart(
    client: TestClient, db_session
):
    """Adding the same SKU a second time should increase quantity, not duplicate the row."""
    user_id = "test_user_inc"
    sku_id = "sku-inc-001"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=100.0)

    # Add quantity=2
    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)
    b2b_client.get_skus_by_ids = AsyncMock(return_value={sku_id: mock_data})
    client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 2},
        headers={"X-Test-User-Id": user_id},
    )

    # Verify one row exists with quantity=2
    result = await db_session.execute(
        select(CartItemModel).where(
            (CartItemModel.user_id == user_id) &
            (CartItemModel.sku_id == sku_id)
        )
    )
    item = result.scalar_one()
    assert item.quantity == 2

    # Add quantity=3 more
    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)
    b2b_client.get_skus_by_ids = AsyncMock(return_value={sku_id: mock_data})
    client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 3},
        headers={"X-Test-User-Id": user_id},
    )

    # Refresh and check quantity is now 5
    await db_session.refresh(item)
    assert item.quantity == 5

    # Verify only one row exists
    all_items = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    items = all_items.scalars().all()
    assert len(items) == 1


# =====================================================================
# 2. get_cart_enriched_with_b2b_data
# =====================================================================

@pytest.mark.asyncio
async def test_get_cart_enriched_with_b2b_data(
    client: TestClient, db_session
):
    """GET /cart should return items enriched with B2B product data."""
    user_id = "test_user_enrich"
    sku_id = "sku-enrich-001"

    # Insert cart item directly into DB
    cart_item = CartItemModel(
        user_id=user_id, sku_id=sku_id, quantity=3
    )
    db_session.add(cart_item)
    await db_session.commit()

    mock_data = _mock_sku_ok(
        sku_id=sku_id,
        price=2500.0,
        product_id="prod-enrich",
        product_title="Enriched Product",
    )

    b2b_client.get_skus_by_ids = AsyncMock(return_value={sku_id: mock_data})
    response = client.get("/api/v1/cart", headers={"X-Test-User-Id": user_id})

    assert response.status_code == 200
    data = response.json()
    assert data["total_items"] == 1
    assert data["total_amount"] == 2500.0 * 3  # price * quantity
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["sku_id"] == sku_id
    assert item["product_id"] == "prod-enrich"
    assert item["product_title"] == "Enriched Product"
    assert item["quantity"] == 3
    assert item["subtotal"] == 7500.0
    assert data["unavailable"] == []


# =====================================================================
# 3. unavailable_sku_shown_with_reason
# =====================================================================

@pytest.mark.asyncio
async def test_unavailable_sku_shown_with_reason(
    client: TestClient, db_session
):
    """Unavailable SKUs appear in 'unavailable' list with reason and are excluded from total."""
    user_id = "test_user_unavail"
    available_sku = "sku-avail-001"
    oos_sku = "sku-oos-001"
    inactive_sku = "sku-inactive-001"

    # Insert three items into cart
    for sku in [available_sku, oos_sku, inactive_sku]:
        ci = CartItemModel(user_id=user_id, sku_id=sku, quantity=1)
        db_session.add(ci)
    await db_session.commit()

    skus_data = {
        available_sku: _mock_sku_ok(sku_id=available_sku, price=1000.0),
        oos_sku: _mock_sku_out_of_stock(sku_id=oos_sku, price=500.0),
        inactive_sku: _mock_sku_inactive(sku_id=inactive_sku, price=300.0),
    }

    b2b_client.get_skus_by_ids = AsyncMock(return_value=skus_data)
    response = client.get("/api/v1/cart", headers={"X-Test-User-Id": user_id})

    assert response.status_code == 200
    data = response.json()

    # Only available item in total
    assert data["total_items"] == 1
    assert data["total_amount"] == 1000.0

    # One available item
    assert len(data["items"]) == 1
    assert data["items"][0]["sku_id"] == available_sku

    # Two unavailable items with correct reasons
    unavailable = data["unavailable"]
    assert len(unavailable) == 2

    reasons = {u["sku_id"]: u["unavailable_reason"] for u in unavailable}
    assert reasons[oos_sku] == "out_of_stock"
    assert reasons[inactive_sku] == "not_active"


# =====================================================================
# 4. guest_cart_merged_on_login
# =====================================================================

@pytest.mark.asyncio
async def test_guest_cart_merged_on_login(
    client: TestClient, db_session
):
    """Merge should take MAX(guest_quantity, auth_quantity) for conflicting SKUs."""
    guest_id = "guest-session-abc"
    auth_id = "user-xyz"
    sku_shared = "sku-shared-001"
    sku_guest_only = "sku-guest-only-001"
    sku_auth_only = "sku-auth-only-001"

    # Guest cart: sku_shared qty=5, sku_guest_only qty=3
    db_session.add(CartItemModel(user_id=guest_id, sku_id=sku_shared, quantity=5))
    db_session.add(CartItemModel(user_id=guest_id, sku_id=sku_guest_only, quantity=3))

    # Auth cart: sku_shared qty=2, sku_auth_only qty=7
    db_session.add(CartItemModel(user_id=auth_id, sku_id=sku_shared, quantity=2))
    db_session.add(CartItemModel(user_id=auth_id, sku_id=sku_auth_only, quantity=7))
    await db_session.commit()

    # Before merge: auth cart has 2 items
    auth_before = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == auth_id)
    )
    assert len(auth_before.scalars().all()) == 2

    # Perform merge
    b2b_client.get_skus_by_ids = AsyncMock(return_value={})
    response = client.post(
        f"/api/v1/cart/merge?guest_user_id={guest_id}&auth_user_id={auth_id}"
    )

    assert response.status_code == 200
    data = response.json()

    # After merge: auth cart should have 3 items
    auth_after = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == auth_id)
    )
    items_after = auth_after.scalars().all()
    assert len(items_after) == 3

    # Build lookup for verification
    lookup = {ci.sku_id: ci.quantity for ci in items_after}
    # MAX(5, 2) = 5 for shared SKU
    assert lookup[sku_shared] == 5
    # Guest-only item copied as-is
    assert lookup[sku_guest_only] == 3
    # Auth-only item unchanged
    assert lookup[sku_auth_only] == 7


# =====================================================================
# Extra: IDOR protection — user cannot access another user's cart
# =====================================================================

@pytest.mark.asyncio
async def test_idor_protection_cart_access(
    client: TestClient, db_session
):
    """User A cannot read/modify User B's cart items."""
    user_a = "user-a"
    user_b = "user-b"

    # User B has a cart item
    db_session.add(CartItemModel(user_id=user_b, sku_id="sku-secret", quantity=1))
    await db_session.commit()

    # User A tries to GET B's cart — no header → 401
    response = client.get("/api/v1/cart")
    assert response.status_code == 401

    # User A tries to DELETE B's cart item — no header → 401
    response = client.delete("/api/v1/cart/items/sku-secret")
    assert response.status_code == 401


# =====================================================================
# Extra: query param user_id is ignored (IDOR)
# =====================================================================

@pytest.mark.asyncio
async def test_query_param_user_id_ignored(client: TestClient, db_session):
    """Query param user_id must be ignored — must use X-User-Id header."""
    user_b = "user-b"

    # User B has a cart item
    db_session.add(CartItemModel(user_id=user_b, sku_id="sku-secret", quantity=1))
    await db_session.commit()

    # Trying to access with ?user_id=... should fail (401, no header)
    response = client.get(f"/api/v1/cart?user_id={user_b}")
    assert response.status_code == 401
