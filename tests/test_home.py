"""Tests for home page endpoints — DoD: 3 required pytest cases."""

import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.models import BannerModel, BannerClickModel


# =====================================================================
# 1. active_banners_returned_sorted_by_priority
# =====================================================================

@pytest.mark.asyncio
async def test_active_banners_returned_sorted_by_priority(
    client: TestClient, db_session
):
    """Only active banners within their schedule window are returned, sorted by priority DESC."""
    now = datetime.now(timezone.utc)

    # Banner 1: active, high priority, within window
    db_session.add(BannerModel(
        title="Summer Sale",
        image_url="https://example.com/summer.jpg",
        link_url="https://example.com/summer",
        priority=10,
        is_active=True,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
    ))

    # Banner 2: active, lower priority, within window
    db_session.add(BannerModel(
        title="New Arrivals",
        image_url="https://example.com/new.jpg",
        link_url="https://example.com/new",
        priority=5,
        is_active=True,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
    ))

    # Banner 3: active, but expired (ends_at in the past)
    db_session.add(BannerModel(
        title="Old Promo",
        image_url="https://example.com/old.jpg",
        link_url="https://example.com/old",
        priority=100,  # high priority but expired
        is_active=True,
        starts_at=now - timedelta(days=60),
        ends_at=now - timedelta(days=1),
    ))

    # Banner 4: active, but not yet started
    db_session.add(BannerModel(
        title="Upcoming Sale",
        image_url="https://example.com/upcoming.jpg",
        link_url="https://example.com/upcoming",
        priority=50,
        is_active=True,
        starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=60),
    ))

    # Banner 5: inactive
    db_session.add(BannerModel(
        title="Inactive Banner",
        image_url="https://example.com/inactive.jpg",
        link_url="https://example.com/inactive",
        priority=999,
        is_active=False,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
    ))

    await db_session.commit()

    response = client.get("/api/v1/home/banners")

    assert response.status_code == 200
    data = response.json()

    # Only 2 banners: Summer Sale (priority 10) and New Arrivals (priority 5)
    assert len(data) == 2

    # Sorted by priority DESC
    assert data[0]["title"] == "Summer Sale"
    assert data[0]["priority"] == 10
    assert data[1]["title"] == "New Arrivals"
    assert data[1]["priority"] == 5

    # Expired, not-yet-started, and inactive banners are excluded
    titles = [b["title"] for b in data]
    assert "Old Promo" not in titles
    assert "Upcoming Sale" not in titles
    assert "Inactive Banner" not in titles


# =====================================================================
# 2. no_active_banners_returns_200_empty
# =====================================================================

@pytest.mark.asyncio
async def test_no_active_banners_returns_200_empty(
    client: TestClient, db_session
):
    """When no banners match criteria → 200 with empty list."""
    now = datetime.now(timezone.utc)

    # Only expired banners exist
    db_session.add(BannerModel(
        title="Expired Promo",
        image_url="https://example.com/expired.jpg",
        link_url="https://example.com/expired",
        priority=10,
        is_active=True,
        starts_at=now - timedelta(days=60),
        ends_at=now - timedelta(days=1),
    ))
    await db_session.commit()

    response = client.get("/api/v1/home/banners")

    assert response.status_code == 200
    data = response.json()
    assert data == []


# =====================================================================
# 3. click_on_unknown_banner_returns_400
# =====================================================================

@pytest.mark.asyncio
async def test_click_on_unknown_banner_returns_400(
    client: TestClient, db_session
):
    """Click event for a non-existent banner → 400 Bad Request."""
    response = client.post(
        "/api/v1/banner-events",
        json={"banner_id": 99999, "user_id": "user-123"},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "BANNER_NOT_FOUND"


# =====================================================================
# 4. click_on_known_banner_returns_204
# =====================================================================

@pytest.mark.asyncio
async def test_click_on_known_banner_returns_204(
    client: TestClient, db_session
):
    """Click on a valid banner returns 204 and persists the event."""
    now = datetime.now(timezone.utc)

    banner = BannerModel(
        title="Test Banner",
        image_url="https://example.com/test.jpg",
        link_url="https://example.com/test",
        priority=1,
        is_active=True,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
    )
    db_session.add(banner)
    await db_session.commit()

    response = client.post(
        "/api/v1/banner-events",
        json={"banner_id": banner.banner_id, "user_id": "user-456"},
    )

    assert response.status_code == 204

    # Verify click was recorded
    result = await db_session.execute(
        select(BannerClickModel).where(
            BannerClickModel.banner_id == banner.banner_id
        )
    )
    clicks = result.scalars().all()
    assert len(clicks) == 1
    assert clicks[0].user_id == "user-456"


# =====================================================================
# 5. guest_click_without_user_id_returns_204
# =====================================================================

@pytest.mark.asyncio
async def test_guest_click_without_user_id_returns_204(
    client: TestClient, db_session
):
    """Guests can click without user_id — banner_id is required, user_id is nullable."""
    now = datetime.now(timezone.utc)

    banner = BannerModel(
        title="Guest Banner",
        image_url="https://example.com/guest.jpg",
        link_url="https://example.com/guest",
        priority=1,
        is_active=True,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
    )
    db_session.add(banner)
    await db_session.commit()

    response = client.post(
        "/api/v1/banner-events",
        json={"banner_id": banner.banner_id},
    )

    assert response.status_code == 204

    # Verify click was recorded with null user_id
    result = await db_session.execute(
        select(BannerClickModel).where(
            BannerClickModel.banner_id == banner.banner_id
        )
    )
    clicks = result.scalars().all()
    assert len(clicks) == 1
    assert clicks[0].user_id is None


# =====================================================================
# 6. banners_without_schedule_are_always_shown
# =====================================================================

@pytest.mark.asyncio
async def test_banners_without_schedule_are_always_shown(
    client: TestClient, db_session
):
    """Banners with no starts_at/ends_at are always shown (if active)."""
    db_session.add(BannerModel(
        title="Always On",
        image_url="https://example.com/always.jpg",
        link_url="https://example.com/always",
        priority=1,
        is_active=True,
        # No starts_at, no ends_at
    ))
    await db_session.commit()

    response = client.get("/api/v1/home/banners")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["title"] == "Always On"
