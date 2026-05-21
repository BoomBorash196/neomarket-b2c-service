"""B2B API client for communicating with Seller module."""

import httpx
from typing import Optional
from src.config import settings


class B2BClient:
    """Client for B2B API operations."""

    def __init__(self):
        self.base_url = settings.B2B_API_URL

    async def get_product_by_id(self, product_id: str) -> Optional[dict]:
        """Get product details from B2B."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/products/{product_id}",
                timeout=10.0
            )
            if response.status_code == 200:
                return response.json()
            return None

    async def get_skus_by_ids(self, sku_ids: list[str]) -> dict[str, dict]:
        """Get multiple SKU details from B2B."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/skus/batch",
                json={"sku_ids": sku_ids},
                timeout=10.0
            )
            if response.status_code == 200:
                return response.json()
            return {}

    async def get_sku_by_id(self, sku_id: str) -> Optional[dict]:
        """Get single SKU details from B2B."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/skus/{sku_id}",
                timeout=10.0
            )
            if response.status_code == 200:
                return response.json()
            return None

    async def get_categories(self) -> list[dict]:
        """Get category tree from B2B."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/categories",
                timeout=10.0
            )
            if response.status_code == 200:
                return response.json()
            return []

    async def get_products_by_category(
        self,
        category_id: str,
        page: int = 1,
        page_size: int = 20,
        filters: Optional[dict] = None
    ) -> dict:
        """Get products filtered by category and optional filters."""
        async with httpx.AsyncClient() as client:
            params = {"page": page, "page_size": page_size}
            if filters:
                params.update(filters)
            
            response = await client.get(
                f"{self.base_url}/products",
                params=params,
                timeout=10.0
            )
            if response.status_code == 200:
                return response.json()
            return {"products": [], "total": 0}

    async def reserve_stock(self, reservations: list[dict]) -> dict:
        """Reserve stock in B2B for order creation."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/reserve",
                json={"reservations": reservations},
                timeout=10.0
            )
            if response.status_code == 200:
                return response.json()
            raise Exception(f"Stock reservation failed: {response.text}")


b2b_client = B2BClient()
