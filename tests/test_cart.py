"""Tests for B2C customer cart endpoints — US-CART-03."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.models import CartItemModel
from src.services.b2b_client import b2b_client


def _auth_headers(user_id: str, session_id: str | None = None) -> dict:
    """Trusted identity headers: authenticated buyer or guest session."""
    headers = {"X-User-Id": user_id}
    if session_id is not None:
        headers["X-Session-Id"] = session_id
    return headers


def _mock_sku_ok(
    sku_id: str = "sku-001",
    price: float = 1000.0,
    quantity_available: int = 10,
    is_active: bool = True,
    product_id: str = "prod-001",
    product_title: str = "Test Product",
) -> dict:
    """B2B SKU-shaped response used by the cart enrichment contract."""
    return {
        "id": sku_id,
        "product_id": product_id,
        "name": product_title,
        "price": int(price),
        "discount": 0,
        "stock_quantity": quantity_available,
        "active_quantity": quantity_available,
        "article": None,
        "images": [],
        "characteristics": [],
        # B2C cart mapping tolerates both the B2B field and a legacy alias.
        "is_active": is_active,
        "quantity_available": quantity_available,
    }


# =====================================================================
# 1. add_sku_increments_quantity_if_already_in_cart
# =====================================================================

@pytest.mark.asyncio
async def test_add_sku_increments_quantity_if_already_in_cart(client: TestClient, db_session):
    """Adding the same SKU a second time should increase quantity, not duplicate the row."""
    user_id = "test_user_inc"
    sku_id = "sku-inc-001"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=100.0, quantity_available=10)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)
    b2b_client.get_skus_by_ids = AsyncMock(return_value={sku_id: mock_data})

    response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 2},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 201

    response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 3},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 200

    result = await db_session.execute(
        select(CartItemModel).where(
            (CartItemModel.user_id == user_id) & (CartItemModel.sku_id == sku_id)
        )
    )
    item = result.scalar_one()
    assert item.quantity == 5

    all_items = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    assert len(all_items.scalars().all()) == 1


# =====================================================================
# 2. get_cart_enriched_with_b2b_data
# =====================================================================

@pytest.mark.asyncio
async def test_get_cart_enriched_with_b2b_data(client: TestClient, db_session):
    """GET /cart should return items enriched with B2B product data."""
    user_id = "test_user_enrich"
    sku_id = "sku-enrich-001"

    db_session.add(CartItemModel(user_id=user_id, sku_id=sku_id, quantity=3))
    await db_session.commit()

    mock_data = _mock_sku_ok(
        sku_id=sku_id,
        price=2500.0,
        quantity_available=10,
        product_id="prod-enrich",
        product_title="Enriched Product",
    )
    b2b_client.get_skus_by_ids = AsyncMock(return_value={sku_id: mock_data})

    response = client.get("/api/v1/cart", headers=_auth_headers(user_id))

    assert response.status_code == 200
    data = response.json()
    assert data["items_count"] == 3
    assert data["total_items"] == 3
    assert data["subtotal"] == 2500.0 * 3
    assert data["total_amount"] == 2500.0 * 3
    assert data["is_valid"] is True
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["sku_id"] == sku_id
    assert item["product_id"] == "prod-enrich"
    assert item["name"] == "Enriched Product"
    assert item["product_title"] == "Enriched Product"
    assert item["sku_name"] == "Enriched Product"
    assert item["quantity"] == 3
    assert item["subtotal"] == 7500.0
    assert item["name"] == "Enriched Product"
    assert item["unit_price"] == 2500
    assert item["line_total"] == 7500
    assert item["available_quantity"] == 10
    assert item["available_stock"] == 10
    assert item["is_available"] is True
    assert item["available"] is True
    assert item["item_id"] == item["cart_item_id"]
    assert data["unavailable"] == []

    # Canonical summary / checkout payload.
    assert data["summary"]["total_amount"] == 7500
    assert data["summary"]["total_items"] == 1
    assert data["summary"]["total_quantity"] == 3
    assert data["summary"]["available_items"] == 1
    assert data["summary"]["has_unavailable_items"] is False
    assert data["summary"]["checkout_ready"] is True
    assert data["summary"]["currency"] == "RUB"
    assert data["checkout_payload"]["total_amount"] == 7500
    assert len(data["checkout_payload"]["items"]) == 1
    assert data["checkout_payload"]["items"][0]["sku_id"] == sku_id


# =====================================================================
# 3. unavailable_sku_shown_with_reason
# =====================================================================

@pytest.mark.asyncio
async def test_unavailable_sku_shown_with_reason(client: TestClient, db_session):
    """Unavailable SKUs appear in unavailable list with reason and are excluded from totals."""
    user_id = "test_user_unavail"
    available_sku = "sku-avail-001"
    oos_sku = "sku-oos-001"
    inactive_sku = "sku-inactive-001"

    for sku in (available_sku, oos_sku, inactive_sku):
        db_session.add(CartItemModel(user_id=user_id, sku_id=sku, quantity=1))
    await db_session.commit()

    skus_data = {
        available_sku: _mock_sku_ok(sku_id=available_sku, price=1000.0, quantity_available=10),
        oos_sku: _mock_sku_ok(sku_id=oos_sku, price=500.0, quantity_available=0, is_active=True),
        inactive_sku: _mock_sku_ok(sku_id=inactive_sku, price=300.0, quantity_available=5, is_active=False),
    }
    b2b_client.get_skus_by_ids = AsyncMock(return_value=skus_data)

    response = client.get("/api/v1/cart", headers=_auth_headers(user_id))

    assert response.status_code == 200
    data = response.json()
    # The cart spec requires every row in ``items`` and in the quantity count,
    # including unavailable rows. Only available lines contribute to money.
    assert data["items_count"] == 3
    assert data["total_items"] == 3
    assert data["total_amount"] == 1000.0
    assert data["subtotal"] == 1000.0
    assert data["is_valid"] is False
    assert len(data["items"]) == 3

    items_by_sku = {item["sku_id"]: item for item in data["items"]}
    assert items_by_sku[available_sku]["is_available"] is True
    assert items_by_sku[available_sku]["available"] is True

    oos_item = items_by_sku[oos_sku]
    assert oos_item["is_available"] is False
    assert oos_item["available"] is False
    assert oos_item["unavailable_reason"] == "OUT_OF_STOCK"
    assert oos_item["available_stock"] == 0
    assert oos_item["available_quantity"] == 0
    assert oos_item["line_total"] == 0

    inactive_item = items_by_sku[inactive_sku]
    assert inactive_item["is_available"] is False
    assert inactive_item["available"] is False
    assert inactive_item["unavailable_reason"] == "SKU_DISABLED"
    assert inactive_item["line_total"] == 0

    unavailable = data["unavailable"]
    assert len(unavailable) == 2
    reasons = {item["sku_id"]: item["unavailable_reason"] for item in unavailable}
    assert reasons[oos_sku] == "OUT_OF_STOCK"
    assert reasons[inactive_sku] == "SKU_DISABLED"

    assert data["summary"]["total_amount"] == 1000
    assert data["summary"]["total_items"] == 3
    assert data["summary"]["total_quantity"] == 3
    assert data["summary"]["available_items"] == 1
    assert data["summary"]["has_unavailable_items"] is True
    assert data["summary"]["checkout_ready"] is False
    assert len(data["checkout_payload"]["items"]) == 1


# =====================================================================
# 4. guest_cart_merged_on_login
# =====================================================================

@pytest.mark.asyncio
async def test_guest_cart_merged_on_login(client: TestClient, db_session):
    """Merge takes MAX(guest_quantity, auth_quantity) for conflicting SKUs."""
    guest_id = "guest-session-abc"
    auth_id = "user-xyz"
    shared_sku = "sku-shared-001"
    guest_only_sku = "sku-guest-only-001"
    auth_only_sku = "sku-auth-only-001"

    db_session.add(CartItemModel(user_id=guest_id, sku_id=shared_sku, quantity=5))
    db_session.add(CartItemModel(user_id=guest_id, sku_id=guest_only_sku, quantity=3))
    db_session.add(CartItemModel(user_id=auth_id, sku_id=shared_sku, quantity=2))
    db_session.add(CartItemModel(user_id=auth_id, sku_id=auth_only_sku, quantity=7))
    await db_session.commit()

    b2b_client.get_skus_by_ids = AsyncMock(
        return_value={
            shared_sku: _mock_sku_ok(sku_id=shared_sku, price=100.0, quantity_available=10),
            guest_only_sku: _mock_sku_ok(sku_id=guest_only_sku, price=100.0, quantity_available=10),
            auth_only_sku: _mock_sku_ok(sku_id=auth_only_sku, price=100.0, quantity_available=10),
        }
    )

    response = client.post(
        "/api/v1/cart/merge",
        headers=_auth_headers(auth_id, guest_id),
    )

    assert response.status_code == 200
    data = response.json()

    result = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == auth_id)
    )
    items_after = result.scalars().all()
    assert len(items_after) == 3

    lookup = {item.sku_id: item.quantity for item in items_after}
    assert lookup[shared_sku] == 5
    assert lookup[guest_only_sku] == 3
    assert lookup[auth_only_sku] == 7
    assert data["is_valid"] is True

    guest_after = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == guest_id)
    )
    assert guest_after.scalars().all() == []


# =====================================================================
# Extra: cart validation and contract-level behaviour
# =====================================================================

@pytest.mark.asyncio
async def test_cart_validate_returns_unavailable_issue(client: TestClient, db_session):
    """POST /cart/validate returns is_valid=false and an unavailable issue."""
    user_id = "test_user_validate"
    sku_id = "sku-validate-001"
    db_session.add(CartItemModel(user_id=user_id, sku_id=sku_id, quantity=2))
    await db_session.commit()

    b2b_client.get_skus_by_ids = AsyncMock(
        return_value={sku_id: _mock_sku_ok(sku_id=sku_id, price=100.0, quantity_available=0)}
    )

    response = client.post("/api/v1/cart/validate", headers=_auth_headers(user_id))

    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert len(data["issues"]) == 1
    assert data["issues"][0]["sku_id"] == sku_id
    assert data["issues"][0]["type"] == "OUT_OF_STOCK"
    # Unavailable rows stay in the main list and in the quantity count.
    assert len(data["cart"]["items"]) == 1
    assert data["cart"]["items"][0]["sku_id"] == sku_id
    assert data["cart"]["items"][0]["is_available"] is False
    assert data["cart"]["items"][0]["line_total"] == 0
    assert data["cart"]["items_count"] == 2
    assert data["cart"]["subtotal"] == 0


@pytest.mark.asyncio
async def test_add_sku_quantity_exceeded_returns_409(client: TestClient, db_session):
    """Adding more than available stock returns the documented 409 conflict."""
    user_id = "test_user_quantity"
    sku_id = "sku-quantity-001"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=100.0, quantity_available=3)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)
    b2b_client.get_skus_by_ids = AsyncMock(return_value={sku_id: mock_data})

    response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 2},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 201

    response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 2},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "QUANTITY_EXCEEDED"


@pytest.mark.asyncio
async def test_first_add_exceeding_available_returns_409(client: TestClient, db_session):
    """The first add must also respect available stock, not only increments."""
    user_id = "test_user_first_add_exceeded"
    sku_id = "sku-first-add-exceeded"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=100.0, quantity_available=3)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)

    response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 5},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "QUANTITY_EXCEEDED"

    result = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_zero_active_quantity_does_not_fall_back_to_stock_quantity(
    client: TestClient, db_session
):
    """``active_quantity = 0`` means out of stock even if stock_quantity > 0."""
    user_id = "test_user_zero_active"
    sku_id = "sku-zero-active"
    db_session.add(CartItemModel(user_id=user_id, sku_id=sku_id, quantity=1))
    await db_session.commit()

    zero_active_sku = {
        "id": sku_id,
        "product_id": "prod-zero-active",
        "name": "Zero Active Product",
        "price": 100,
        "discount": 0,
        # Total warehouse stock is positive, but nothing is sellable.
        "stock_quantity": 10,
        "active_quantity": 0,
        "article": None,
        "images": [],
        "characteristics": [],
    }
    b2b_client.get_skus_by_ids = AsyncMock(return_value={sku_id: zero_active_sku})
    b2b_client.get_sku_by_id = AsyncMock(return_value=zero_active_sku)

    response = client.get("/api/v1/cart", headers=_auth_headers(user_id))
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["available_quantity"] == 0
    assert item["available_stock"] == 0
    assert item["is_available"] is False
    assert item["unavailable_reason"] == "OUT_OF_STOCK"
    assert item["line_total"] == 0

    response = client.post(
        "/api/v1/cart/items",
        json={"sku_id": sku_id, "quantity": 1},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "SKU_OUT_OF_STOCK"
    """PATCH /cart/items/{sku_id} accepts quantity in the JSON body."""
    user_id = "test_user_update"
    sku_id = "sku-update-001"
    db_session.add(CartItemModel(user_id=user_id, sku_id=sku_id, quantity=2))
    await db_session.commit()

    b2b_client.get_sku_by_id = AsyncMock(
        return_value=_mock_sku_ok(sku_id=sku_id, price=100.0, quantity_available=10)
    )
    b2b_client.get_skus_by_ids = AsyncMock(
        return_value={sku_id: _mock_sku_ok(sku_id=sku_id, price=100.0, quantity_available=10)}
    )

    response = client.patch(
        f"/api/v1/cart/items/{sku_id}",
        json={"quantity": 5},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 200
    assert response.json()["items_count"] == 5

    result = await db_session.execute(
        select(CartItemModel).where(
            (CartItemModel.user_id == user_id) & (CartItemModel.sku_id == sku_id)
        )
    )
    assert result.scalar_one().quantity == 5


@pytest.mark.asyncio
async def test_update_cart_item_quantity_exceeded_returns_409(client: TestClient, db_session):
    """PATCH with a quantity above available stock returns 409."""
    user_id = "test_user_update_exceeded"
    sku_id = "sku-update-exceeded"
    db_session.add(CartItemModel(user_id=user_id, sku_id=sku_id, quantity=1))
    await db_session.commit()

    b2b_client.get_sku_by_id = AsyncMock(
        return_value=_mock_sku_ok(sku_id=sku_id, price=100.0, quantity_available=2)
    )

    response = client.patch(
        f"/api/v1/cart/items/{sku_id}",
        json={"quantity": 3},
        headers=_auth_headers(user_id),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "QUANTITY_EXCEEDED"


@pytest.mark.asyncio
async def test_sku_lookup_uses_b2b_active_quantity(client: TestClient, db_session):
    """Cart maps B2B ``active_quantity`` into buyer availability."""
    user_id = "test_user_b2b_fields"
    sku_id = "sku-b2b-fields"
    db_session.add(CartItemModel(user_id=user_id, sku_id=sku_id, quantity=2))
    await db_session.commit()

    b2b_client.get_skus_by_ids = AsyncMock(
        return_value={
            sku_id: {
                "id": sku_id,
                "product_id": "prod-b2b",
                "name": "B2B Product",
                "price": 100,
                "discount": 0,
                "stock_quantity": 2,
                "active_quantity": 2,
                "article": None,
                "images": [],
                "characteristics": [],
            }
        }
    )

    response = client.get("/api/v1/cart", headers=_auth_headers(user_id))

    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is True
    assert data["items"][0]["available_quantity"] == 2
    assert data["items"][0]["is_available"] is True


# =====================================================================
# Extra: cart identity must come from headers, not query
# =====================================================================

@pytest.mark.asyncio
async def test_cart_requires_owner_header(client: TestClient):
    """Cart operations without an owner header return 401 in the Error envelope."""
    response = client.get("/api/v1/cart")
    assert response.status_code == 401
    assert response.json()["code"] == "MISSING_AUTH"


@pytest.mark.asyncio
async def test_idor_protection_cart_access(client: TestClient, db_session):
    """A buyer cannot read or delete another buyer's cart using spoofable IDs."""
    user_a = "user-a"
    user_b = "user-b"
    sku_id = "sku-secret"

    db_session.add(CartItemModel(user_id=user_b, sku_id=sku_id, quantity=1))
    await db_session.commit()

    response = client.get("/api/v1/cart", headers=_auth_headers(user_a))
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["items_count"] == 0
    assert data["subtotal"] == 0
    assert data["is_valid"] is True

    response = client.delete(f"/api/v1/cart/items/{sku_id}", headers=_auth_headers(user_a))
    assert response.status_code == 404
