# src/order_placer.py
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

import requests

from src.kalshi_client import KalshiClient, KalshiAPIError
from src.market_scanner import QualifyingMarket

if TYPE_CHECKING:
    from src.strategies.base import OrderParams

logger = logging.getLogger(__name__)

# HTTP status codes worth retrying
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def build_market_url(ticker: str, event_ticker: str = "", title: str = "") -> str:
    """Build Kalshi market URL.

    Format: https://kalshi.com/markets/{event_ticker}/{title-slug}/{ticker}

    Args:
        ticker: Market ticker (e.g., KXTRUMPSAY-26FEB09)
        event_ticker: Event ticker (e.g., KXTRUMPSAY)
        title: Market title for slug generation

    Returns:
        Full market URL
    """
    ticker_lower = ticker.lower()

    if not event_ticker:
        # Fallback: just use ticker
        return f"https://kalshi.com/markets/{ticker_lower}"

    event_ticker_lower = event_ticker.lower()

    if title:
        # Convert title to slug: lowercase, replace spaces/special chars with hyphens
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return f"https://kalshi.com/markets/{event_ticker_lower}/{slug}/{ticker_lower}"
    else:
        return f"https://kalshi.com/markets/{event_ticker_lower}/{ticker_lower}"


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    market_ticker: str
    success: bool
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = False
    strategy: Optional[str] = None
    price_cents: Optional[int] = None
    quantity: Optional[int] = None


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is worth retrying."""
    if isinstance(exc, KalshiAPIError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return False


class OrderPlacer:
    """Places limit orders on No contracts for qualifying markets."""

    def __init__(
        self,
        client: KalshiClient,
        contract_count: Optional[int] = None,
        dry_run: bool = False,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ):
        self.client = client
        self.contract_count = contract_count
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

    def _retry(self, fn: Callable, ticker: str) -> Dict[str, Any]:
        """Execute fn with exponential backoff on retryable errors.

        Raises the original exception if all retries are exhausted or
        the error is not retryable.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt == self.max_retries:
                    raise
                delay = self.retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Order attempt {attempt}/{self.max_retries} failed for "
                    f"{ticker}: {exc} — retrying in {delay:.1f}s",
                    exc_info=True,
                )
                time.sleep(delay)
        raise last_exc  # unreachable, but satisfies type checker

    def place_order(self, market: QualifyingMarket) -> OrderResult:
        """Place a limit buy order for No contracts."""
        client_order_id = str(uuid.uuid4())

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would place order: {market.ticker} "
                f"Buy {self.contract_count} No @ ${market.no_bid/100:.2f}"
            )
            return OrderResult(
                market_ticker=market.ticker,
                success=True,
                order_id=f"dry-run-{client_order_id[:8]}",
                dry_run=True,
            )

        try:
            result = self._retry(
                lambda: self.client.create_order(
                    ticker=market.ticker,
                    side="no",
                    action="buy",
                    count=self.contract_count,
                    order_type="limit",
                    no_price=market.no_bid,
                    client_order_id=client_order_id,
                ),
                ticker=market.ticker,
            )

            order = result.get("order", {})
            order_id = order.get("order_id", order.get("id"))

            logger.info(
                f"Order placed: {market.ticker} "
                f"Buy {self.contract_count} No @ ${market.no_bid/100:.2f} "
                f"(order_id: {order_id})"
            )

            return OrderResult(
                market_ticker=market.ticker,
                success=True,
                order_id=order_id,
            )

        except KalshiAPIError as e:
            logger.error(f"Order failed for {market.ticker}: {e.message}", exc_info=True)
            return OrderResult(
                market_ticker=market.ticker,
                success=False,
                error=e.message,
            )
        except Exception as e:
            logger.error(f"Unexpected error for {market.ticker}: {str(e)}", exc_info=True)
            return OrderResult(
                market_ticker=market.ticker,
                success=False,
                error=str(e),
            )

    def place_order_from_params(
        self,
        params: "OrderParams",
        strategy: str,
    ) -> OrderResult:
        """Place order using OrderParams from a strategy."""
        client_order_id = str(uuid.uuid4())

        # Extract market info for URL building
        snapshot = params.market_snapshot or {}
        event_ticker = snapshot.get("event_ticker", "")
        title = snapshot.get("title", "")
        market_url = build_market_url(params.ticker, event_ticker, title)

        # Get additional market details for logging
        yes_bid = snapshot.get("_yes_bid") or snapshot.get("yes_bid")
        if yes_bid and yes_bid > 1:
            yes_bid = yes_bid / 100

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would place order: {params.ticker} "
                f"Buy {params.quantity} {params.side.upper()} @ {params.price_cents}c"
            )
            logger.info(f"  Market: {title or params.ticker}")
            if yes_bid:
                logger.info(f"  YES bid: ${yes_bid:.2f} | NO price: ${params.price_cents/100:.2f}")
            logger.info(f"  URL: {market_url}")
            return OrderResult(
                market_ticker=params.ticker,
                success=True,
                order_id=f"dry-run-{client_order_id[:8]}",
                client_order_id=client_order_id,
                dry_run=True,
                strategy=strategy,
                price_cents=params.price_cents,
                quantity=params.quantity,
            )

        try:
            order_kwargs = {
                "ticker": params.ticker,
                "side": params.side,
                "action": params.action,
                "count": params.quantity,
                "order_type": "limit",
                "client_order_id": client_order_id,
                "post_only": params.post_only,
            }

            if params.side == "no":
                order_kwargs["no_price"] = params.price_cents
            else:
                order_kwargs["yes_price"] = params.price_cents

            result = self._retry(
                lambda: self.client.create_order(**order_kwargs),
                ticker=params.ticker,
            )

            order = result.get("order", {})
            order_id = order.get("order_id", order.get("id"))

            logger.info(
                f"Order placed: {params.ticker} "
                f"Buy {params.quantity} {params.side.upper()} @ {params.price_cents}c "
                f"(order_id: {order_id})"
            )
            logger.info(f"  Market: {title or params.ticker}")
            logger.info(f"  URL: {market_url}")

            return OrderResult(
                market_ticker=params.ticker,
                success=True,
                order_id=order_id,
                client_order_id=client_order_id,
                strategy=strategy,
                price_cents=params.price_cents,
                quantity=params.quantity,
            )

        except KalshiAPIError as e:
            logger.error(f"Order failed for {params.ticker}: {e.message}", exc_info=True)
            yes_bid = snapshot.get("yes_bid")
            yes_ask = snapshot.get("yes_ask")
            no_bid = snapshot.get("no_bid")
            no_ask = snapshot.get("no_ask")
            logger.error(
                f"  Order price: {params.price_cents}c {params.side.upper()} | "
                f"yes_bid={yes_bid} yes_ask={yes_ask} no_bid={no_bid} no_ask={no_ask}",
                exc_info=True,
            )
            return OrderResult(
                market_ticker=params.ticker,
                success=False,
                client_order_id=client_order_id,
                error=e.message,
                strategy=strategy,
            )
        except Exception as e:
            logger.error(f"Unexpected error for {params.ticker}: {str(e)}", exc_info=True)
            return OrderResult(
                market_ticker=params.ticker,
                success=False,
                client_order_id=client_order_id,
                error=str(e),
                strategy=strategy,
            )
