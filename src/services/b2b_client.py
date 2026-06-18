"""B2B API client for communicating with Seller module."""

import httpx
from typing import Optional
from src.config import settings


class B2BClientError(Exception):
    """Raised when B2B API is unavailable or returns an error."""

    def __init__(self, status_code: int, message: str = "B2B service unavailable"):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class B2BClient:
    """Client for B2B API operations."""

    def __init__(self):
        self.base_url = settings.B2B_API_URL
        self.timeout = 10.0

    def _headers(self) -> dict:
        """Return headers for inter-service auth."""
        return {"X-Service-Key": settings.B2B_SERVICE_KEY}

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        """Execute an HTTP request to B2B and return parsed JSON, or raise B2BClientError."""
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return response.json()
            raise B2BClientError(
                status_code=response.status_code,
                message=f"B2B {method} {path} returned {response.status_code}: {response.text}",
            )

    async def get_product_by_id(self, product_id: str) -> Optional[dict]:
        """Get product details from B2B."""
        try:
            return await self._request("GET", f"/products/{product_id}")
        except B2BClientError:
            return None

    async def get_skus_by_ids(self, sku_ids: list[str]) -> dict[str, dict]:
        """Get multiple SKU details from B2B."""
        try:
            resp = await self._request("POST", "/skus/batch", json_body={"sku_ids": sku_ids})
            return resp if isinstance(resp, dict) else {}
        except B2BClientError:
            return {}

    async def get_sku_by_id(self, sku_id: str) -> Optional[dict]:
        """Get single SKU details from B2B."""
        try:
            return await self._request("GET", f"/skus/{sku_id}")
        except B2BClientError:
            return None

    async def get_categories(self) -> list[dict]:
        """Get category tree from B2B."""
        try:
            return await self._request("GET", "/categories")
        except B2BClientError:
            return []

    async def get_products(
        self,
        category_id: Optional[str] = None,
        search: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock: Optional[bool] = None,
        brand: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = "asc",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Get products with filtering, sorting, and pagination.

        Only returns products with status = MODERATED, deleted = false, active_quantity > 0.
        """
        params: dict = {"page": page, "page_size": page_size}
        if category_id:
            params["category_id"] = category_id
        if search:
            params["search"] = search
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price
        if in_stock is not None:
            params["in_stock"] = in_stock
        if brand:
            params["brand"] = brand
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order

        try:
            result = await self._request("GET", "/products", params=params)
            return result
        except B2BClientError as exc:
            # Re-raise so the caller can map to 502/503
            raise exc

    async def get_facets(
        self,
        category_id: Optional[str] = None,
        search: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock: Optional[bool] = None,
        facet_fields: Optional[list[str]] = None,
    ) -> dict:
        """Get facet counts from B2B."""
        params: dict = {}
        if category_id:
            params["category_id"] = category_id
        if search:
            params["search"] = search
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price
        if in_stock is not None:
            params["in_stock"] = in_stock
        if facet_fields:
            params["facet_fields"] = facet_fields

        try:
            return await self._request("GET", "/catalog/facets", params=params)
        except B2BClientError as exc:
            raise exc

    async def get_products_batch(self, product_ids: list[int]) -> dict[str, dict]:
        """Get multiple products by IDs (batch request)."""
        try:
            resp = await self._request("POST", "/products/batch", json_body={"product_ids": product_ids})
            return resp if isinstance(resp, dict) else {}
        except B2BClientError:
            return {}

    async def reserve_stock(self, reservations: list[dict]) -> dict:
        """Reserve stock in B2B for order creation."""
        try:
            return await self._request("POST", "/reserve", json_body={"reservations": reservations})
        except B2BClientError as exc:
            raise exc


b2b_client = B2BClient()