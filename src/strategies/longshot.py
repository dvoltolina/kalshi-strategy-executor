# src/strategies/longshot.py
"""Longshot strategy - buy NO on sports markets with low YES prices."""
import logging
from typing import Any, Dict, List, Optional

from src.strategies.base import BaseStrategy, OrderParams
from src.market_utils import (
    exclude_disqualified_markets,
    filter_by_yes_price,
    filter_by_no_price,
    filter_by_series_category,
    exclude_multivariate,
    exclude_tickers_containing,
    normalize_market_prices,
)

logger = logging.getLogger(__name__)


class LongshotStrategy(BaseStrategy):
    """Buy NO on sports markets where YES is priced as a longshot."""

    def __init__(self, client, config: Dict[str, Any]):
        super().__init__(client, config)
        self._series_cache: Dict[str, str] = {}

    def find_markets(self) -> List[Dict[str, Any]]:
        """Find qualifying sports longshot markets."""
        filters = self.config["filters"]

        # Use mve_filter=exclude at API level if excluding multivariate
        mve_filter = "exclude" if filters.get("exclude_multivariate", True) else None

        all_markets = []
        cursor = None
        while True:
            result = self.client.get_markets(
                status="open",
                limit=1000,
                cursor=cursor,
                mve_filter=mve_filter,
            )
            markets = result.get("markets", [])
            all_markets.extend(markets)
            cursor = result.get("cursor")
            if not cursor or not markets:
                break

        logger.info(f"Fetched {len(all_markets)} open markets (multivariate excluded at API level)")

        normalize_market_prices(all_markets)
        all_markets = exclude_disqualified_markets(all_markets)

        if filters.get("exclude_tickers_containing"):
            all_markets = exclude_tickers_containing(
                all_markets, filters["exclude_tickers_containing"]
            )
            logger.info(f"  {len(all_markets)} after excluding tickers")

        all_markets = filter_by_yes_price(
            all_markets,
            filters["min_yes_price"],
            filters["max_yes_price"],
        )
        logger.info(f"  {len(all_markets)} in YES price range")

        min_no = 1.0 - filters["max_yes_price"]
        max_no = 1.0 - filters["min_yes_price"]
        all_markets = filter_by_no_price(all_markets, min_no, max_no)
        logger.info(f"  {len(all_markets)} with NO bid in range")

        all_markets = filter_by_series_category(
            all_markets,
            filters["categories"],
            self.client,
            self._series_cache,
        )
        logger.info(f"  {len(all_markets)} in target categories")

        return all_markets

    def calculate_order(self, market: Dict[str, Any]) -> Optional[OrderParams]:
        """Calculate order for a qualifying market."""
        order_cfg = self.config["order"]

        no_bid = market.get("_no_bid")
        if no_bid is None:
            return None

        pricing = order_cfg.get("pricing", {})
        mode = pricing.get("mode", "at_bid")

        if mode == "at_bid":
            price_cents = no_bid
        elif mode == "beat_bid":
            increment = int(pricing.get("increment", 0.01) * 100)
            price_cents = no_bid + increment
            # Cap at no_ask - 1 to avoid post-only cross on tight spreads
            no_ask = market.get("no_ask")
            if no_ask is not None:
                if no_ask > 1:
                    no_ask_cents = no_ask
                else:
                    no_ask_cents = int(no_ask * 100)
                if price_cents >= no_ask_cents:
                    logger.debug(
                        f"{market['ticker']}: beat_bid {price_cents}c would cross "
                        f"no_ask {no_ask_cents}c, falling back to {no_ask_cents - 1}c"
                    )
                    price_cents = no_ask_cents - 1
        else:
            price_cents = no_bid

        return OrderParams(
            ticker=market["ticker"],
            side=order_cfg.get("side", "no"),
            action="buy",
            price_cents=price_cents,
            quantity=order_cfg["contracts_per_market"],
            post_only=order_cfg.get("post_only", True),
            market_snapshot=market,
        )
