"""
Polymarket Data API async client.
Covers leaderboard, trades, positions, value, and activity endpoints.
All requests use exponential backoff retry (3 retries).
"""

import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://data-api.polymarket.com"
_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: Optional[dict] = None,
) -> Any:
    """
    Execute a GET request with retry logic.

    Args:
        client: Shared httpx.AsyncClient instance.
        path: URL path (appended to _BASE).
        params: Query parameters dict.

    Returns:
        Parsed JSON response body.

    Raises:
        httpx.HTTPStatusError: If all retries exhausted with non-2xx status.
    """
    url = f"{_BASE}{path}"
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            resp = await client.get(url, params=params, timeout=15.0)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            last_exc = exc
            wait = _BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                "Data API request failed (attempt %d/%d) %s: %s. Retrying in %.1fs",
                attempt + 1,
                _RETRIES,
                url,
                exc,
                wait,
            )
            await asyncio.sleep(wait)
    raise last_exc  # type: ignore[misc]


class DataApiClient:
    """Async client for the Polymarket Data API."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "DataApiClient":
        self._client = httpx.AsyncClient(
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Client not started. Use async context manager.")
        return self._client

    async def get_leaderboard(
        self,
        category: str = "OVERALL",
        time_period: str = "ALL",
        order_by: str = "PNL",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Fetch the Polymarket leaderboard.

        Args:
            category: Market category filter (OVERALL, POLITICS, SPORTS, CRYPTO, etc.)
            time_period: Time window (ALL, MONTH, WEEK, DAY)
            order_by: Sort field (PNL, VOL)
            limit: Maximum results to return (max 50).
            offset: Pagination offset (max 1000).

        Returns:
            List of leaderboard entry dicts.
        """
        data = await _get(
            self._ensure_client(),
            "/v1/leaderboard",
            params={
                "category": category,
                "timePeriod": time_period,
                "orderBy": order_by,
                "limit": limit,
                "offset": offset,
            },
        )
        if isinstance(data, list):
            return data
        return data.get("data", data.get("leaderboard", []))

    async def get_trades(
        self,
        user: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Fetch trade history for a user address.

        Args:
            user: On-chain wallet address.
            limit: Maximum results.
            offset: Pagination offset.

        Returns:
            List of trade dicts.
        """
        data = await _get(
            self._ensure_client(),
            "/trades",
            params={"user": user, "limit": limit, "offset": offset},
        )
        if isinstance(data, list):
            return data
        return data.get("data", [])

    async def get_positions(self, user: str) -> list[dict]:
        """
        Fetch open positions for a user address.

        Args:
            user: On-chain wallet address.

        Returns:
            List of position dicts.
        """
        data = await _get(
            self._ensure_client(),
            "/positions",
            params={"user": user},
        )
        if isinstance(data, list):
            return data
        return data.get("data", [])

    async def get_value(self, user: str) -> dict:
        """
        Fetch portfolio value info for a user.

        Args:
            user: On-chain wallet address.

        Returns:
            Dict with value fields.
        """
        data = await _get(
            self._ensure_client(),
            "/value",
            params={"user": user},
        )
        if isinstance(data, dict):
            return data
        return {}

    async def get_activity(self, user: str, limit: int = 20) -> list[dict]:
        """
        Fetch activity feed for a user.

        Args:
            user: On-chain wallet address.
            limit: Maximum results.

        Returns:
            List of activity event dicts.
        """
        data = await _get(
            self._ensure_client(),
            "/activity",
            params={"user": user, "limit": limit},
        )
        if isinstance(data, list):
            return data
        return data.get("data", [])
