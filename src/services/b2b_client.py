"""B2B API client for communicating with the Seller module.

Catalog methods follow the published B2B contract (``b2b/openapi.yaml``,
tag ``Public Catalog``) literally — parameter names, sort enum values and
response schemas all come from the spec:

* ``GET /public/products`` — params: ``category_id``, ``search``, ``min_price``,
  ``max_price``, ``seller_id``, ``filters[...]``, ``sort``,
  ``limit``, ``offset`` → ``ProductPublicPaginatedResponse``
* ``GET /public/products/{id}`` → ``ProductPublicResponse``
* ``GET /public/products/{id}/similar?limit=`` → ``[ProductPublicShortResponse]``
* ``POST /public/products/batch`` — body ``{"product_ids": [...]}``
  → ``[ProductPublicResponse]``

Base URL is expected to carry the ``/api/v1`` prefix
(``settings.B2B_API_URL = http://host.docker.internal:8001/api/v1``).

Any transport-level problem (DNS, connect refused, read timeout) is turned into
``B2BClientError`` so callers can answer the buyer with 502 instead of leaking a
500 from the framework.
"""

import logging
from typing import Any, Mapping, Optional, Sequence

import httpx

from src.config import settings
from src.services.b2b_public_catalog import (
    B2B_SORT_VALUES,
    B2BPayloadError,
    B2BProduct,
    B2BProductPage,
    B2BProductShort,
    parse_product,
    parse_product_page,
    parse_product_shorts,
    parse_products,
)

logger = logging.getLogger(__name__)


class B2BClientError(Exception):
    """Raised when the B2B API is unavailable or answers off-contract.

    ``kind`` lets the caller distinguish causes without string matching:
    ``timeout`` | ``connect`` | ``http`` | ``protocol`` | ``decode``.
    """

    def __init__(self, status_code: int, message: str = "B2B service unavailable", kind: str = "http"):
        self.status_code = status_code
        self.message = message
        self.kind = kind
        super().__init__(message)


class B2BClient:
    """Client for B2B API operations."""

    def __init__(self) -> None:
        self.base_url = settings.B2B_API_URL
        self.timeout = 10.0

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        """Return headers for inter-service auth (X-Service-Key per B2B spec)."""
        return {"X-Service-Key": settings.B2B_SERVICE_KEY}

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        """Execute an HTTP request to B2B and return parsed JSON.

        Raises:
            B2BClientError — on transport failure, non-2xx status, undecodable
            body or malformed JSON.
        """
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
        except httpx.TimeoutException as exc:
            raise B2BClientError(
                status_code=504,
                message=f"B2B timeout after {self.timeout}s on {method} {path}",
                kind="timeout",
            ) from exc
        except httpx.RequestError as exc:
            raise B2BClientError(
                status_code=502,
                message=f"B2B unreachable on {method} {path}: {type(exc).__name__}",
                kind="connect",
            ) from exc

        if response.status_code == 404:
            raise B2BClientError(
                status_code=404,
                message=f"B2B {method} {path} returned 404",
                kind="http",
            )
        if response.status_code >= 400:
            raise B2BClientError(
                status_code=response.status_code,
                message=f"B2B {method} {path} returned {response.status_code}: {response.text[:500]}",
                kind="http",
            )

        try:
            return response.json()
        except ValueError as exc:
            raise B2BClientError(
                status_code=502,
                message=f"B2B {method} {path} returned a non-JSON body",
                kind="decode",
            ) from exc

    async def _request_parsed(self, method: str, path: str, parser, **kwargs) -> Any:
        """Request + read the body through a strict ``b2b_public_catalog`` parser."""
        payload = await self._request(method, path, **kwargs)
        try:
            return parser(payload)
        except B2BPayloadError as exc:
            raise B2BClientError(
                status_code=502,
                message=f"B2B {method} {path} payload does not match the B2B schema: {exc}",
                kind="protocol",
            ) from exc

    # ------------------------------------------------------------------
    # Public catalog — US-CAT-01 / 03 / 04
    # ------------------------------------------------------------------
    @staticmethod
    def _filters_params(filters: Optional[Mapping[str, Any]]) -> list[tuple[str, str]]:
        """Serialise B2B dynamic filters into ``filters[key]=value`` pairs.

        B2B declares ``filters`` as ``style: deepObject, explode: true`` with
        values of type string / array of string / number / boolean.
        """
        pairs: list[tuple[str, str]] = []
        for key, value in (filters or {}).items():
            if value is None or value == "":
                continue
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    pairs.append((f"filters[{key}]", str(item)))
            elif isinstance(value, bool):
                pairs.append((f"filters[{key}]", "true" if value else "false"))
            else:
                pairs.append((f"filters[{key}]", str(value)))
        return pairs

    async def list_public_products(
        self,
        *,
        category_id: Optional[str] = None,
        search: Optional[str] = None,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        seller_id: Optional[str] = None,
        filters: Optional[Mapping[str, Any]] = None,
        sort: str = "created_desc",
        limit: int = 20,
        offset: int = 0,
    ) -> B2BProductPage:
        """``GET /public/products`` — storefront listing.

        B2B applies the visibility rule itself: only ``status = MODERATED``,
        ``deleted = false`` and ``active_quantity > 0`` products are returned,
        so B2C never filters on those fields itself.
        """
        if sort not in B2B_SORT_VALUES:
            raise ValueError(
                f"sort '{sort}' is not in the B2B sort enum {list(B2B_SORT_VALUES)}"
            )

        params: list[tuple[str, str]] = [("limit", str(limit)), ("offset", str(offset))]
        if category_id:
            params.append(("category_id", str(category_id)))
        if search:
            params.append(("search", str(search)))
        if min_price is not None:
            params.append(("min_price", str(int(min_price))))
        if max_price is not None:
            params.append(("max_price", str(int(max_price))))
        if seller_id:
            params.append(("seller_id", str(seller_id)))
        if sort:
            params.append(("sort", sort))
        params.extend(self._filters_params(filters))

        return await self._request_parsed(
            "GET", "/public/products", parse_product_page, params=params
        )

    async def get_public_product(self, product_id: str) -> Optional[B2BProduct]:
        """``GET /public/products/{id}`` → ``ProductPublicResponse`` or ``None`` (404)."""
        try:
            return await self._request_parsed(
                "GET", f"/public/products/{product_id}", parse_product
            )
        except B2BClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_public_similar_products(
        self, product_id: str, limit: int = 10
    ) -> list[B2BProductShort]:
        """``GET /public/products/{id}/similar?limit=`` → ``[ProductPublicShortResponse]``.

        Note: the published B2B operation takes **only** ``product_id`` and
        ``limit`` — there is no ``category_id`` parameter, so category-scoped
        or parent-category expansion cannot be requested from B2B.
        """
        params = [("limit", str(limit))]
        return await self._request_parsed(
            "GET",
            f"/public/products/{product_id}/similar",
            lambda payload: parse_product_shorts(payload, path="similar"),
            params=params,
        )

    async def batch_public_products(self, product_ids: Sequence[str]) -> list[B2BProduct]:
        """``POST /public/products/batch`` → ``[ProductPublicResponse]``.

        Used for facet attributes: the public listing returns
        ``ProductPublicShortResponse`` (no characteristics), and brand values
        live in ``characteristics`` of the full card.
        """
        ids = [str(pid) for pid in product_ids if pid]
        if not ids:
            return []
        if len(ids) > 100:
            raise ValueError("B2B /public/products/batch accepts at most 100 product_ids")
        return await self._request_parsed(
            "POST",
            "/public/products/batch",
            lambda payload: parse_products(payload, path="products"),
            json_body={"product_ids": ids},
        )

    # ------------------------------------------------------------------
    # Legacy / non-catalog helpers (kept for the cart, wishlist and order
    # contracts — they are tracked as a follow-up to align them with the
    # published B2B schema as well).
    # ------------------------------------------------------------------
    async def get_product_by_id(self, product_id: str) -> Optional[dict]:
        """Raw storefront card (dict) — used by wishlist / home / recommendations."""
        try:
            return await self._request("GET", f"/public/products/{product_id}")
        except B2BClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_skus_by_ids(self, sku_ids: list[str]) -> dict[str, dict]:
        """Get multiple SKU details from B2B.

        The published B2B contract exposes only ``GET /public/skus/{sku_id}`` —
        there is no batch SKU endpoint. So this walks the per-SKU endpoint for
        the requested ids and returns ``{sku_id: sku}`` for the ones that exist.

        Individual misses are skipped; a transport failure is raised so callers
        can answer 502 rather than silently treating the SKUs as absent.
        """
        result: dict[str, dict] = {}
        if not sku_ids:
            return result
        for sku_id in sku_ids:
            try:
                sku = await self.get_sku_by_id(sku_id)
            except B2BClientError as exc:
                if exc.kind == "connect" or exc.kind == "timeout":
                    raise
                continue
            if sku is not None:
                result[str(sku_id)] = sku
        return result

    async def get_sku_by_id(self, sku_id: str) -> Optional[dict]:
        """``GET /public/skus/{sku_id}`` → ``SKUPublicResponse`` or ``None`` (404)."""
        try:
            return await self._request("GET", f"/public/skus/{sku_id}")
        except B2BClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_categories(self) -> list[dict]:
        """Get category list from B2B.

        Caveat: the published B2B contract only declares ``/api/v1/categories``
        with ``HTTPBearer`` (seller auth) — there is no public categories
        endpoint for ``X-Service-Key``. Tracked as a protocol question.
        """
        try:
            result = await self._request("GET", "/categories")
            return result if isinstance(result, list) else []
        except B2BClientError:
            return []

    async def get_products_batch(self, product_ids: list[str]) -> dict[str, dict]:
        """Get multiple products by IDs (batch request)."""
        try:
            resp = await self._request("POST", "/products/batch", json_body={"product_ids": product_ids})
            return resp if isinstance(resp, dict) else {}
        except B2BClientError:
            return {}

    async def get_products_by_ids(self, product_ids: list[str]) -> dict[str, dict]:
        """Get multiple products by string IDs (batch request).

        Returns a dict mapping product_id -> product_data.
        Returns empty dict on failure.
        """
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
        """Get products filtered by category (used by /recommendations)."""
        params: dict = {"category_id": category_id, "page": page, "page_size": page_size}
        try:
            return await self._request("GET", "/public/products", params=params)
        except B2BClientError:
            return {"products": [], "total": 0}

    async def reserve_stock(self, reservations: list[dict]) -> dict:
        """Reserve stock in B2B for order creation.

        B2B returns:
        {
          "success": [{"sku_id": int, "reserved": int, "remaining": int}, ...],
          "failed": [{"sku_id": int, "reason": str, ...}, ...],
          "total_reserved": int,
          "total_failed": int
        }
        """
        try:
            return await self._request("POST", "/inventory/reserve", json_body={"reservations": reservations})
        except B2BClientError as exc:
            raise exc


b2b_client = B2BClient()