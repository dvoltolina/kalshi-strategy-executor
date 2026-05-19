# src/market_scanner.py
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.kalshi_client import KalshiClient

if TYPE_CHECKING:
    from src.state_manager import StateManager

logger = logging.getLogger(__name__)


@dataclass
class QualifyingMarket:
    """A market that qualifies for order placement."""
    ticker: str
    event_ticker: str
    title: str
    yes_bid: float
    no_bid: int  # In cents (1-99)
    category: str


class MarketScanner:
    """Scans Kalshi markets to find qualifying sports longshots."""

    def __init__(
        self,
        client: KalshiClient,
        sports_categories: List[str],
        min_yes_price: float,
        max_yes_price: float,
        min_no_price: float,
        max_no_price: float,
        state_manager: Optional["StateManager"] = None,
    ):
        self.client = client
        self.sports_categories = [c.lower() for c in sports_categories]
        self.min_yes_price = min_yes_price
        self.max_yes_price = max_yes_price
        self.min_no_price = min_no_price
        self.max_no_price = max_no_price
        self.state_manager = state_manager
        self._event_cache: Dict[str, Dict[str, Any]] = {}

        # Load event cache from state if available
        if state_manager:
            self._event_cache = state_manager.load_event_cache()
            if self._event_cache:
                logger.info(f"Loaded {len(self._event_cache)} cached events from previous run")

    def _get_event(self, event_ticker: str) -> Dict[str, Any]:
        """Get event details with caching."""
        if event_ticker in self._event_cache:
            logger.debug(f"Cache hit for event {event_ticker}")
            return self._event_cache[event_ticker]

        logger.debug(f"Fetching event {event_ticker} from API")
        result = self.client.get_event(event_ticker)
        self._event_cache[event_ticker] = result.get("event", {})

        # Persist cache after each new fetch
        if self.state_manager:
            self.state_manager.save_event_cache(self._event_cache)

        return self._event_cache[event_ticker]

    def _filter_by_category(self, markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter markets to only those in sports categories."""
        filtered = []
        total = len(markets)

        for i, market in enumerate(markets, 1):
            # Log progress every 100 markets
            if i % 100 == 0 or i == total:
                logger.info(f"Checking categories: {i}/{total} ({len(filtered)} qualifying so far)")
                if self.state_manager:
                    self.state_manager.save_progress(i, total)

            event_ticker = market.get("event_ticker")
            if not event_ticker:
                continue

            event = self._get_event(event_ticker)
            category = event.get("category", "").lower()
            if category in self.sports_categories:
                market["_category"] = category
                filtered.append(market)

                # Save qualifying market incrementally
                if self.state_manager:
                    self.state_manager.append_qualifying_market({
                        "ticker": market["ticker"],
                        "event_ticker": event_ticker,
                        "title": market.get("title", market["ticker"]),
                        "yes_bid": market.get("_yes_bid"),
                        "no_bid": market.get("_no_bid"),
                        "category": category,
                    })

        return filtered

    def _filter_mention_tickers(self, markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove markets with MENTION in ticker."""
        return [m for m in markets if "MENTION" not in m.get("ticker", "").upper()]

    def _filter_multivariate(self, markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove multivariate event markets."""
        return [m for m in markets if not m.get("is_mve", False)]

    def _filter_by_price(self, markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter markets by yes price range."""
        filtered = []
        for market in markets:
            yes_bid = market.get("yes_bid")
            if yes_bid is None:
                yes_bid_dollars = market.get("yes_bid_dollars", {})
                if isinstance(yes_bid_dollars, dict):
                    yes_bid_str = yes_bid_dollars.get("value")
                    if yes_bid_str:
                        try:
                            yes_bid = float(yes_bid_str)
                        except ValueError:
                            continue
            if yes_bid is None:
                continue
            # Convert cents to dollars if needed (values > 1 are likely cents)
            if yes_bid > 1:
                yes_bid = yes_bid / 100
            if self.min_yes_price <= yes_bid <= self.max_yes_price:
                market["_yes_bid"] = yes_bid
                filtered.append(market)
        return filtered

    def _filter_has_no_bid(self, markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter to markets that have no_bid in the target range."""
        filtered = []
        for market in markets:
            no_bid = market.get("no_bid")
            if no_bid is None:
                no_bid_dollars = market.get("no_bid_dollars", {})
                if isinstance(no_bid_dollars, dict):
                    no_bid_str = no_bid_dollars.get("value")
                    if no_bid_str:
                        try:
                            no_bid = float(no_bid_str)
                        except ValueError:
                            continue
            if no_bid is None or no_bid <= 0:
                continue
            # Convert to dollars if in cents (values > 1 are likely cents)
            if no_bid > 1:
                no_bid_dollars_val = no_bid / 100
            else:
                no_bid_dollars_val = no_bid
            # Check if no_bid is in range
            if self.min_no_price <= no_bid_dollars_val <= self.max_no_price:
                market["_no_bid"] = int(no_bid_dollars_val * 100)
                filtered.append(market)
        return filtered

    def _fetch_all_markets(self) -> List[Dict[str, Any]]:
        """Fetch all open markets with pagination."""
        all_markets = []
        cursor = None
        while True:
            result = self.client.get_markets(status="open", limit=1000, cursor=cursor)
            markets = result.get("markets", [])
            all_markets.extend(markets)
            cursor = result.get("cursor")
            if not cursor or not markets:
                break
            logger.debug(f"Fetched {len(all_markets)} markets so far...")
        return all_markets

    def scan(self) -> List[QualifyingMarket]:
        """Scan all markets and return qualifying ones."""
        logger.info("Fetching all open markets...")
        markets = self._fetch_all_markets()
        logger.info(f"Found {len(markets)} open markets")

        # Apply free filters first (no API calls) to minimize event lookups
        logger.info("Excluding multivariate event markets...")
        markets = self._filter_multivariate(markets)
        logger.info(f"  {len(markets)} after excluding multivariate")

        logger.info("Excluding MENTION tickers...")
        markets = self._filter_mention_tickers(markets)
        logger.info(f"  {len(markets)} after excluding MENTION")

        logger.info(f"Filtering by price range (${self.min_yes_price:.2f}-${self.max_yes_price:.2f})...")
        markets = self._filter_by_price(markets)
        logger.info(f"  {len(markets)} in price range")

        logger.info(f"Filtering by no_bid range (${self.min_no_price:.2f}-${self.max_no_price:.2f})...")
        markets = self._filter_has_no_bid(markets)
        logger.info(f"  {len(markets)} with no_bid in range")

        # Category filter last (requires API call per unique event)
        logger.info("Filtering by sports categories...")
        markets = self._filter_by_category(markets)
        logger.info(f"  {len(markets)} qualifying markets")

        return [
            QualifyingMarket(
                ticker=m["ticker"],
                event_ticker=m.get("event_ticker", ""),
                title=m.get("title", m["ticker"]),
                yes_bid=m["_yes_bid"],
                no_bid=m["_no_bid"],
                category=m.get("_category", ""),
            )
            for m in markets
        ]
