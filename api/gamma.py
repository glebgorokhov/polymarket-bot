"""
Polymarket Gamma API async client.
Covers markets, events, and profiles endpoints.
All requests use exponential backoff retry (3 retries).
"""

import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://gamma-api.polymarket.com"
_RETRIES = 3
_BACKOFF_BASE = 1.0


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: Optional[dict] = None,
) -> Any:
    """
    Execute a GET request against the Gamma API with retry.

    Args:
        client: Shared httpx.AsyncClient instance.
        path: URL path.
        params: Query parameters.

    Returns:
        Parsed JSON response.
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
                "Gamma API request failed (attempt %d/%d) %s: %s. Retrying in %.1fs",
                attempt + 1,
                _RETRIES,
                url,
                exc,
                wait,
            )
            await asyncio.sleep(wait)
    raise last_exc  # type: ignore[misc]


class GammaApiClient:
    """Async client for the Polymarket Gamma API."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "GammaApiClient":
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

    async def get_markets(
        self,
        condition_id: Optional[str] = None,
        active: Optional[bool] = None,
        closed: Optional[bool] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Fetch markets from Gamma API.

        Args:
            condition_id: Filter by specific condition ID.
            active: Filter for active/inactive markets.
            closed: Filter for closed markets.
            limit: Max results.

        Returns:
            List of market dicts.
        """
        params: dict[str, Any] = {"limit": limit}
        if condition_id is not None:
            params["condition_id"] = condition_id
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()

        data = await _get(self._ensure_client(), "/markets", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", data.get("markets", []))

    async def get_market(self, condition_id: str) -> Optional[dict]:
        """
        Fetch a single market by condition ID.

        Args:
            condition_id: The market condition ID.

        Returns:
            Market dict or None if not found.
        """
        markets = await self.get_markets(condition_id=condition_id, limit=1)
        return markets[0] if markets else None

    async def get_events(
        self,
        event_id: Optional[str] = None,
        slug: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """
        Fetch events from Gamma API.

        Args:
            event_id: Filter by event ID.
            slug: Filter by event slug.
            limit: Max results.

        Returns:
            List of event dicts.
        """
        params: dict[str, Any] = {"limit": limit}
        if event_id is not None:
            params["id"] = event_id
        if slug is not None:
            params["slug"] = slug

        data = await _get(self._ensure_client(), "/events", params=params)
        if isinstance(data, list):
            return data
        return data.get("data", data.get("events", []))

    async def get_profiles(self, address: str) -> Optional[dict]:
        """
        Fetch a user profile from the Gamma API.

        Args:
            address: On-chain wallet address.

        Returns:
            Profile dict or None if not found.
        """
        data = await _get(
            self._ensure_client(), "/profiles", params={"address": address}
        )
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """
        Get the current midpoint price for a token from market data.

        Args:
            token_id: The token/outcome ID.

        Returns:
            Midpoint price as float or None.
        """
        try:
            markets = await self.get_markets(limit=1)
            # Search for token within market tokens list
            for market in markets:
                tokens = market.get("tokens", [])
                for token in tokens:
                    if token.get("token_id") == token_id:
                        price = token.get("price")
                        if price is not None:
                            return float(price)
            return None
        except Exception as exc:
            logger.error("Failed to get midpoint for token %s: %s", token_id, exc)
            return None
