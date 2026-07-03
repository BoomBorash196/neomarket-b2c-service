"""B2B API client for communicating with Seller module."""

import logging
import httpx
from typing import Optional
from src.config import settings

logger = logging.getLogger(__name__)


class B2BClientError(Exception):
    """Raised when B2B API is unavailable or returns an error."""

    def __init__(self, status_code: int, message: str = "B2B service unavailable"):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class B2BClient:
    """Client for B2B API operations.

    Uses a shared httpx.AsyncClient for connection pooling and keep-alive.
    Separates public endpoints (no auth) from internal endpoints (X-Service-Key).
    """

    def __init__(self):
        self.base_url = settings.B2B_API_URL
        self.timeout = 10.0
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize shared client for connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self):
        """Close the shared HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _internal_headers(self) -> dict:
        """Headers for inter-service authenticated endpoints."""
        return {"X-Service-Key": settings.B2B_SERVICE_KEY}

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> dict:
        """Execute an HTTP request to B2B and return parsed JSON, or raise B2BClientError."""
        client = await self._get_client()
        response = await client.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=json_body,
            headers=headers or {},
        )
        if response.status_code == 200:
            return response.json()
        raise B2BClientError(
            status_code=response.status_code,
            message=f"B2B {method} {path} returned {response.status_code}: {response.text}",
        )

    # ------------------------------------------------------------------
    # Public endpoints (no service key)
    # ------------------------------------------------------------------

    async def get_product_by_id(self, product_id: str) -> Optional[dict]:
        """Get product details from B2B (public catalog endpoint)."""
        try:
            return await self._request("GET", f"/public/products/{product_id}")
        except B2BClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_sku_by_id(self, sku_id: str) -> Optional[dict]:
        """Get single SKU details from B2B (public endpoint)."""
        try:
            return await self._request("GET", f"/public/skus/{sku_id}")
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
        """Get products with filtering, sorting, and pagination."""
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
            return await self._request("GET", "/public/products", params=params)
        except B2BClientError as exc:
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

    async def get_products_by_ids(self, product_ids: list[str]) -> dict[str, dict]:
        """Get multiple products by string IDs (public batch endpoint)."""
        if not product_ids:
            return {}
        try:
            resp = await self._request(
                "POST", "/public/products/batch",
                json_body={"product_ids": product_ids},
            )
            return resp if isinstance(resp, dict) else {}
        except B2BClientError:
            return {}

    async def get_products_by_category(
        self,
        category_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Get products filtered by category."""
        params: dict = {"category_id": category_id, "page": page, "page_size": page_size}
        try:
            return await self._request("GET", "/public/products", params=params)
        except B2BClientError:
            return {"products": [], "total": 0}

    async def get_similar_products(
        self,
        product_id: str,
        category_id: str,
        limit: int = 8,
    ) -> dict:
        """Get similar products from B2B (proxy to B2B algorithm)."""
        params: dict = {
            "product_id": product_id,
            "category_id": category_id,
            "limit": limit,
        }
        try:
            return await self._request("GET", "/catalog/similar", params=params)
        except B2BClientError as exc:
            raise exc

    # ------------------------------------------------------------------
    # Internal endpoints (require X-Service-Key)
    # ------------------------------------------------------------------

    async def get_skus_by_ids(self, sku_ids: list[str]) -> dict[str, dict]:
        """Get multiple SKU details from B2B (internal batch endpoint)."""
        try:
            resp = await self._request(
                "POST", "/public/skus/batch",
                json_body={"sku_ids": sku_ids},
                headers=self._internal_headers(),
            )
            return resp if isinstance(resp, dict) else {}
        except B2BClientError:
            return {}

    async def fulfill_stock(self, order_id: str, reservations: list[dict]) -> dict:
        """Finalize stock reservation — deduct reserved quantity on delivery."""
        try:
            return await self._request(
                "POST", "/inventory/fulfill",
                json_body={"order_id": order_id, "reservations": reservations},
                headers=self._internal_headers(),
            )
        except B2BClientError as exc:
            raise exc

    async def reserve_stock(self, reservations: list[dict]) -> dict:
        """Reserve stock in B2B for order creation."""
        try:
            return await self._request(
                "POST", "/inventory/reserve",
                json_body={"reservations": reservations},
                headers=self._internal_headers(),
            )
        except B2BClientError as exc:
            raise exc


b2b_client = B2BClient()