"""
CLOB API wrapper around py-clob-client.
Provides async-compatible order placement, cancellation, and balance queries.
Uses signature_type=1 (EIP-712) with relayer as funder.
"""

import asyncio
import logging
from functools import partial
from typing import Any, Optional

logger = logging.getLogger(__name__)

_RETRIES = 3
_BACKOFF_BASE = 1.0


async def _run_sync(func, *args, **kwargs) -> Any:
    """Run a synchronous CLOB client call in the default thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


class ClobApiClient:
    """
    Async wrapper around the py-clob-client ClobClient.

    All blocking SDK calls are dispatched to a thread pool executor
    so the asyncio event loop is never blocked.
    """

    def __init__(
        self,
        relayer_api_key: str,
        relayer_api_address: str,
        signer_address: str,
        private_key: str = "",
        relayer_api_secret: str = "",
        relayer_api_passphrase: str = "",
        funder_address: str = "",
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
    ) -> None:
        """
        Initialize the CLOB client wrapper.

        Args:
            relayer_api_key: API key for the relayer.
            relayer_api_address: Wallet address used as the funder/relayer.
            signer_address: Wallet that signs orders.
            host: CLOB endpoint URL.
            chain_id: EVM chain ID (137 = Polygon mainnet).
        """
        self._private_key = private_key  # hex private key for signing orders
        self._relayer_api_key = relayer_api_key
        self._relayer_api_secret = relayer_api_secret
        self._relayer_api_passphrase = relayer_api_passphrase
        self._relayer_api_address = relayer_api_address
        self._signer_address = signer_address
        # funder_address = the account wallet that holds USDC (may differ from signer)
        self._funder_address = funder_address or relayer_api_address
        self._host = host
        self._chain_id = chain_id
        self._client = None

    def _ensure_client(self):
        """Lazily initialize the underlying CLOB client."""
        if self._client is None:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds

                if not self._private_key:
                    raise RuntimeError(
                        "PRIVATE_KEY is required to sign orders. "
                        "Set the PRIVATE_KEY environment variable."
                    )

                # Use private key as the signing key (L1)
                # signature_type=1 = POLY_PROXY (Magic Link / Google login)
                # signature_type=2 = GNOSIS_SAFE (MetaMask login — most common)
                # funder = the proxy wallet address shown on polymarket.com profile
                creds = None
                if self._relayer_api_secret and self._relayer_api_passphrase:
                    creds = ApiCreds(
                        api_key=self._relayer_api_key,
                        api_secret=self._relayer_api_secret,
                        api_passphrase=self._relayer_api_passphrase,
                    )

                # signature_type=2 (POLY_GNOSIS_SAFE) — EOA signs on behalf of proxy wallet
                # Used when account was created via MetaMask/Phantom on polymarket.com
                # funder = proxy wallet address (0x13D4...) that holds USDC
                # key = EOA private key (Phantom wallet, 0xab4f...) that controls the proxy
                self._client = ClobClient(
                    host=self._host,
                    chain_id=self._chain_id,
                    key=self._private_key,
                    creds=creds,
                    signature_type=2,
                    funder=self._funder_address,
                )

                # If no L2 creds provided, derive them from the private key
                if creds is None:
                    try:
                        derived = self._client.create_or_derive_api_creds()
                        self._client.set_api_creds(derived)
                        logger.info("Derived L2 API credentials from private key")
                    except Exception as exc:
                        logger.warning("Could not derive L2 creds: %s — L1-only mode", exc)
            except ImportError as exc:
                raise RuntimeError(
                    "py-clob-client is not installed. Add it to requirements.txt."
                ) from exc
        return self._client

    async def _with_retry(self, func, *args, **kwargs) -> Any:
        """Execute a sync CLOB operation in thread pool with retry logic."""
        last_exc: Optional[Exception] = None
        for attempt in range(_RETRIES):
            try:
                client = self._ensure_client()
                return await _run_sync(func, client, *args, **kwargs)
            except Exception as exc:
                last_exc = exc
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "CLOB request failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    _RETRIES,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def get_balance(self) -> float:
        """
        Return the available USDC (collateral) balance.

        Uses get_balance_allowance(asset_type=COLLATERAL) — the correct
        py-clob-client method (there is no get_balance() on ClobClient).
        """
        def _call(client) -> float:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = client.get_balance_allowance(params)
            if isinstance(result, dict):
                # returns {"balance": "49.0", "allowance": "..."}
                return float(result.get("balance", 0))
            return float(result or 0)

        return await self._with_retry(_call)

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """
        Get the current midpoint price for a market token.

        Args:
            token_id: CLOB token/outcome ID.

        Returns:
            Midpoint price as float or None if unavailable.
        """
        def _call(client) -> Optional[float]:
            try:
                result = client.get_midpoint(token_id=token_id)
                if isinstance(result, dict):
                    return float(result.get("mid", 0) or 0)
                return float(result or 0)
            except Exception:
                return None

        return await self._with_retry(_call)

    async def get_spread(self, token_id: str) -> Optional[float]:
        """
        Get the current bid-ask spread for a market token.

        Args:
            token_id: CLOB token/outcome ID.

        Returns:
            Spread as a fraction (0.0–1.0) or None if unavailable.
        """
        def _call(client) -> Optional[float]:
            try:
                result = client.get_spread(token_id=token_id)
                if isinstance(result, dict):
                    return float(result.get("spread", 0) or 0)
                return float(result or 0)
            except Exception:
                return None

        return await self._with_retry(_call)

    async def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
    ) -> dict:
        """
        Place a market order for a token.

        Args:
            token_id: CLOB token ID.
            side: "BUY" or "SELL".
            amount: Amount in USD (BUY) or shares (SELL).

        Returns:
            Order response dict containing order_id and fill info.
        """
        def _call(client) -> dict:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            clob_side = BUY if side.upper() == "BUY" else SELL
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=clob_side,
            )
            signed_order = client.create_market_order(order_args)
            return client.post_order(signed_order, OrderType.FOK)

        return await self._with_retry(_call)

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> dict:
        """
        Place a GTC limit order.

        Args:
            token_id: CLOB token ID.
            side: "BUY" or "SELL".
            price: Limit price (0–1).
            size: Order size in shares.

        Returns:
            Order response dict.
        """
        def _call(client) -> dict:
            from py_clob_client.clob_types import LimitOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            clob_side = BUY if side.upper() == "BUY" else SELL
            order_args = LimitOrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=clob_side,
            )
            signed_order = client.create_limit_order(order_args)
            return client.post_order(signed_order, OrderType.GTC)

        return await self._with_retry(_call)

    async def cancel_order(self, order_id: str) -> dict:
        """
        Cancel an open limit order.

        Args:
            order_id: The order ID to cancel.

        Returns:
            Cancellation response dict.
        """
        def _call(client) -> dict:
            return client.cancel(order_id=order_id)

        return await self._with_retry(_call)

    async def get_open_orders(self) -> list[dict]:
        """
        Retrieve all open orders for the relayer account.

        Returns:
            List of open order dicts.
        """
        def _call(client) -> list:
            result = client.get_orders()
            if isinstance(result, list):
                return result
            return result.get("data", [])

        return await self._with_retry(_call)
