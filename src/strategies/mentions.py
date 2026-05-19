# src/strategies/mentions.py
"""Mentions strategy - buy NO on Mention markets closing soon."""
import logging
import time
from typing import Any, Dict, List, Optional

from src.strategies.base import BaseStrategy, OrderParams
from src.market_utils import (
    exclude_disqualified_markets,
    filter_by_no_price,
    filter_by_series_category,
    normalize_market_prices,
)

logger = logging.getLogger(__name__)


class MentionsStrategy(BaseStrategy):
    """Buy NO on Mention markets closing within a time window."""

    def __init__(self, client, config: Dict[str, Any]):
        super().__init__(client, config)
        self._series_cache: Dict[str, str] = {}

    def find_markets(self) -> List[Dict[str, Any]]:
        """Find qualifying mention markets closing soon."""
        filters = self.config["filters"]

        now = int(time.time())
        min_close_hours = filters.get("min_close_hours", 0)
        max_close_hours = filters.get("max_close_hours", 24)
        min_close_ts = now + (min_close_hours * 3600)
        max_close_ts = now + (max_close_hours * 3600)

        # Use mve_filter=exclude to skip multivariate events at API level
        all_markets = []
        cursor = None
        while True:
            result = self.client.get_markets(
                status="open",
                limit=1000,
                cursor=cursor,
                min_close_ts=min_close_ts,
                max_close_ts=max_close_ts,
                mve_filter="exclude",
            )
            markets = result.get("markets", [])
            all_markets.extend(markets)
            cursor = result.get("cursor")
            if not cursor or not markets:
                break

        logger.info(f"Fetched {len(all_markets)} markets closing in {min_close_hours}-{max_close_hours}h (multivariate excluded)")

        normalize_market_prices(all_markets)
        all_markets = exclude_disqualified_markets(all_markets)

        all_markets = filter_by_series_category(
            all_markets,
            filters["categories"],
            self.client,
            self._series_cache,
            tags=filters.get("tags"),
        )
        logger.info(f"  {len(all_markets)} in Mentions category")

        min_no = filters["min_no_bid_price"]
        max_no = filters["max_no_bid_price"]
        all_markets = filter_by_no_price(all_markets, min_no, max_no)
        logger.info(f"  {len(all_markets)} with NO bid in {int(min_no*100)}-{int(max_no*100)}c range")

        min_vol = filters.get("min_volume", 0)
        if min_vol > 0:
            before = len(all_markets)
            all_markets = [m for m in all_markets if m.get("volume", 0) >= min_vol]
            logger.info(f"  {len(all_markets)} with volume >= {min_vol} (dropped {before - len(all_markets)})")

        excluded = [kw.lower() for kw in filters.get("excluded_keywords", [])]
        if excluded:
            before = len(all_markets)
            all_markets = [
                m for m in all_markets
                if not any(kw in m.get("title", "").lower() for kw in excluded)
            ]
            if before != len(all_markets):
                logger.info(f"  {len(all_markets)} after excluding keywords {excluded} (dropped {before - len(all_markets)})")

        return all_markets

    def calculate_order(self, market: Dict[str, Any]) -> Optional[OrderParams]:
        """Single-order interface — delegates to ladder."""
        orders = self.calculate_orders(market)
        return orders[0] if orders else None

    def calculate_orders(self, market: Dict[str, Any]) -> List[OrderParams]:
        """Build ladder of NO buy orders above best bid.

        In ladder mode: N levels starting at no_bid + start_offset, stepping by 1c.
        In offset mode: single order at no_bid + offset_cents (legacy).
        """
        order_cfg = self.config["order"]
        filters = self.config["filters"]

        no_bid = market.get("_no_bid")
        if no_bid is None:
            return []

        no_ask = market.get("no_ask")
        if no_ask is not None and no_ask <= 1:
            no_ask = int(no_ask * 100)

        pricing = order_cfg.get("pricing", {})
        mode = pricing.get("mode", "offset")
        min_no_cents = round(filters["min_no_bid_price"] * 100)

        if mode == "ladder":
            return self._build_ladder(market, order_cfg, no_bid, no_ask, min_no_cents, pricing)

        # Legacy offset / at_bid mode — single order
        if mode == "offset":
            offset = pricing.get("offset_cents", -2)
            price_cents = no_bid + offset
        elif mode == "at_bid":
            price_cents = no_bid
        else:
            price_cents = no_bid

        if price_cents < min_no_cents:
            logger.debug(f"Skipping {market['ticker']}: price {price_cents}c < min {min_no_cents}c")
            return []

        return [OrderParams(
            ticker=market["ticker"],
            side=order_cfg.get("side", "no"),
            action="buy",
            price_cents=price_cents,
            quantity=order_cfg["contracts_per_market"],
            post_only=order_cfg.get("post_only", True),
            market_snapshot=market,
        )]

    def _build_ladder(
        self, market: Dict[str, Any], order_cfg: Dict[str, Any],
        no_bid: int, no_ask: Optional[int], min_no_cents: int,
        pricing: Dict[str, Any],
    ) -> List[OrderParams]:
        """Build multi-level ladder from best NO bid going downward."""
        levels = pricing.get("levels", 7)
        start_offset = pricing.get("start_offset", 0)
        step = pricing.get("step", -1)
        total_contracts = order_cfg["contracts_per_market"]

        quantities = self._ladder_quantities(levels, total_contracts)
        going_down = step < 0
        orders = []

        for i, qty in enumerate(quantities):
            price = no_bid + start_offset + (i * step)

            # Out of valid range — all subsequent levels are further out
            if price < 1 or price > 99:
                break

            # Drop levels that cross the spread (upward ladders only)
            if no_ask is not None and price >= no_ask:
                if going_down:
                    continue  # Might dip below ask on later levels
                break

            # Drop levels below min price
            if price < min_no_cents:
                if going_down:
                    break  # All subsequent are lower too
                continue

            orders.append(OrderParams(
                ticker=market["ticker"],
                side=order_cfg.get("side", "no"),
                action="buy",
                price_cents=price,
                quantity=qty,
                post_only=order_cfg.get("post_only", True),
                market_snapshot=market,
            ))

        return orders

    @staticmethod
    def _ladder_quantities(levels: int, total: int) -> List[int]:
        """Compute front-heavy geometric allocation across N levels.

        Level 1 gets ~2x the contracts of the last level.
        Uses geometric decay with ratio = (1/2)^(1/(levels-1)).
        """
        import math
        if levels <= 1:
            return [total]

        r = (0.5) ** (1 / (levels - 1))
        weights = [r ** i for i in range(levels)]
        weight_sum = sum(weights)

        # Allocate proportionally, round down, then distribute remainder
        raw = [w / weight_sum * total for w in weights]
        floored = [int(q) for q in raw]
        remainder = total - sum(floored)

        # Give remainder to the largest levels first
        fractions = [(raw[i] - floored[i], i) for i in range(levels)]
        fractions.sort(reverse=True)
        for j in range(remainder):
            floored[fractions[j][1]] += 1

        return floored
