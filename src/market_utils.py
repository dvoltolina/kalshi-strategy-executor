# src/market_utils.py
"""Shared market filtering utilities for strategies."""
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)

_PRICE_FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price")


def normalize_market_prices(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Backfill legacy numeric price fields from *_dollars string fields.

    Kalshi API migrated from integer-cent fields (yes_bid=55) to
    dollar-string fields (yes_bid_dollars='0.5500').  This function
    writes the old-style fields so downstream code keeps working.
    """
    for market in markets:
        for field in _PRICE_FIELDS:
            if market.get(field) is not None:
                continue  # Already has the old field
            dollars_val = market.get(f"{field}_dollars")
            if dollars_val is None:
                continue
            # May be a plain string like '0.5500' or a dict like {'value': '0.5500'}
            if isinstance(dollars_val, dict):
                dollars_val = dollars_val.get("value")
            if dollars_val is None:
                continue
            try:
                market[field] = round(float(dollars_val) * 100)
            except (ValueError, TypeError):
                pass
    return markets


def filter_by_yes_price(
    markets: List[Dict[str, Any]],
    min_price: float,
    max_price: float,
) -> List[Dict[str, Any]]:
    """Filter markets by YES bid price range."""
    filtered = []
    for market in markets:
        yes_bid = market.get("yes_bid")
        if yes_bid is None:
            continue

        if yes_bid > 1:
            yes_bid = yes_bid / 100

        if min_price <= yes_bid <= max_price:
            market["_yes_bid"] = yes_bid
            filtered.append(market)

    return filtered


def filter_by_no_price(
    markets: List[Dict[str, Any]],
    min_price: float,
    max_price: float,
) -> List[Dict[str, Any]]:
    """Filter markets by NO bid price range."""
    filtered = []
    for market in markets:
        no_bid = market.get("no_bid")
        if no_bid is None or no_bid <= 0:
            continue

        if no_bid > 1:
            no_bid_dollars = no_bid / 100
        else:
            no_bid_dollars = no_bid

        if min_price <= no_bid_dollars <= max_price:
            market["_no_bid"] = int(no_bid_dollars * 100)
            filtered.append(market)

    return filtered


_EXCLUDED_TITLE_PHRASES = [
    "does not qualify",
]


def exclude_disqualified_markets(
    markets: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove 'Event does not qualify' markets.

    Filters by ticker suffix (-NQE) and text fields as a safety net.
    """
    before = len(markets)
    filtered = [
        m for m in markets
        if not m.get("ticker", "").endswith("-NQE")
        and not any(
            phrase in field.lower()
            for phrase in _EXCLUDED_TITLE_PHRASES
            for field in (
                m.get("title", ""),
                m.get("subtitle", ""),
                m.get("yes_sub_title", ""),
                m.get("no_sub_title", ""),
            )
        )
    ]
    dropped = before - len(filtered)
    if dropped:
        logger.info(f"  Excluded {dropped} 'does not qualify' markets")
    return filtered


def exclude_multivariate(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove multivariate event markets."""
    return [m for m in markets if not m.get("is_mve", False)]


def exclude_tickers_containing(
    markets: List[Dict[str, Any]],
    substrings: List[str],
) -> List[Dict[str, Any]]:
    """Remove markets with tickers containing any of the substrings (case-insensitive)."""
    substrings_upper = [s.upper() for s in substrings]

    def should_exclude(ticker: str) -> bool:
        ticker_upper = ticker.upper()
        return any(sub in ticker_upper for sub in substrings_upper)

    return [m for m in markets if not should_exclude(m.get("ticker", ""))]


def filter_by_series_category(
    markets: List[Dict[str, Any]],
    categories: List[str],
    client: "KalshiClient",
    series_cache: Optional[Dict[str, str]] = None,
    tags: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Filter markets by series-level category and/or tags.

    Fetches series for each requested category (and tag) and matches markets
    by checking if their event_ticker starts with a known series ticker.
    More reliable than the deprecated event.category field.

    Some series (e.g. sports-mentions) use category="Sports" but
    tags=["Mentions"], so filtering by tags captures these.
    """
    if series_cache is None:
        series_cache = {}

    # Build series ticker -> category lookup for requested categories
    for category in categories:
        if any(v == category.lower() for v in series_cache.values()):
            continue  # Already fetched this category
        logger.debug(f"Fetching series for category: {category}")
        result = client.get_series(category=category)
        for series in result.get("series") or []:
            ticker = series.get("ticker", "")
            if ticker:
                series_cache[ticker] = category.lower()

    # Also fetch series by tags (e.g. sports-mentions tagged "Mentions")
    for tag in tags or []:
        logger.debug(f"Fetching series for tag: {tag}")
        result = client.get_series(tags=tag)
        for series in result.get("series") or []:
            ticker = series.get("ticker", "")
            if ticker and ticker not in series_cache:
                series_cache[ticker] = tag.lower()

    logger.debug(f"Series cache has {len(series_cache)} tickers across {len(categories)} categories")

    filtered = []
    for market in markets:
        event_ticker = market.get("event_ticker", "")
        if not event_ticker:
            continue

        # Match by checking if event_ticker starts with any series ticker
        for series_ticker, category in series_cache.items():
            if event_ticker.startswith(series_ticker):
                market["_category"] = category
                filtered.append(market)
                break

    return filtered


def filter_by_category(
    markets: List[Dict[str, Any]],
    categories: List[str],
    client: "KalshiClient",
    event_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Filter markets by event category (deprecated, use filter_by_series_category)."""
    if event_cache is None:
        event_cache = {}

    categories_lower = [c.lower() for c in categories]
    filtered = []

    for market in markets:
        event_ticker = market.get("event_ticker")
        if not event_ticker:
            continue

        if event_ticker not in event_cache:
            logger.debug(f"Fetching event {event_ticker} from API")
            result = client.get_event(event_ticker)
            event_cache[event_ticker] = result.get("event", {})

        event = event_cache[event_ticker]
        category = event.get("category", "").lower()

        if category in categories_lower:
            market["_category"] = category
            filtered.append(market)

    return filtered
