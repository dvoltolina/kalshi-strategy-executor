# src/order_guard.py
"""Order Guard - Auto-cancel resting orders before events start."""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import requests as http_requests

if TYPE_CHECKING:
    from src.database import Database
    from src.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)

CONFIDENCE_LEVELS = {"high": 3, "medium": 2, "low": 1}

GROK_MODEL = "grok-4-1-fast"
GROK_API_URL = "https://api.x.ai/v1/responses"

SYSTEM_PROMPT = """\
You estimate when real-world events start (game tip-offs, TV airings, speeches, etc.) \
based on prediction market data. You receive event titles, subtitles, categories, \
individual market titles, and close times. Call the report_event_start_times tool \
with your estimates.

IMPORTANT: Use web search and X search to look up the actual scheduled start time for \
each event. Search for specific queries like "NBA Lakers Spurs February 10 2026 tip-off \
time" or "NFL Super Bowl 2026 kickoff time". Always prefer search results over guessing \
from context clues. If search fails or returns nothing useful, fall back to inference.

For each event, find the NEXT FUTURE instance. Use today's date (provided in the user \
message) as your reference point. For example, if the event is "Lakers vs Rockets" and \
today is February 10, find the next scheduled Lakers vs Rockets game on or after that date.

Confidence levels:
- "high": confirmed via web search or title contains an explicit date/time
- "medium": you can reasonably infer from context (e.g., "Tonight's Lakers game" + close time)
- "low": mostly guessing, no search results found

If you truly cannot estimate, use the close_time minus 4 hours as a fallback with "low" confidence.

You MUST call the report_event_start_times tool with your estimates."""

GROK_TOOLS = [
    {"type": "web_search"},
    {"type": "x_search"},
    {
        "type": "function",
        "name": "report_event_start_times",
        "description": "Report estimated start times for prediction market events",
        "parameters": {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "event_ticker": {
                                "type": "string",
                                "description": "The event ticker identifier",
                            },
                            "estimated_start_utc": {
                                "type": "string",
                                "description": "ISO 8601 UTC datetime of estimated event start",
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "reasoning": {
                                "type": "string",
                                "description": "Brief explanation for the estimate",
                            },
                        },
                        "required": [
                            "event_ticker",
                            "estimated_start_utc",
                            "confidence",
                            "reasoning",
                        ],
                    },
                },
            },
            "required": ["events"],
        },
    },
]


class EventCache:
    """DB-backed cache for event start time estimates.

    In-memory dict for fast lookups, persisted to SQLite for crash recovery.
    Milestone-sourced entries are periodically re-checked for schedule changes.
    """

    def __init__(self, db: "Database"):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._db = db
        self._load_from_db()

    def _load_from_db(self):
        """Load persisted estimates into memory on startup."""
        rows = self._db.load_event_estimates()
        loaded = 0
        for row in rows:
            try:
                start_dt = datetime.fromisoformat(
                    row["estimated_start_utc"].replace("Z", "+00:00")
                )
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)

                cached_at = datetime.fromisoformat(
                    row["cached_at"].replace("Z", "+00:00")
                )
                if cached_at.tzinfo is None:
                    cached_at = cached_at.replace(tzinfo=timezone.utc)

                self._cache[row["event_ticker"]] = {
                    "estimated_start_utc": start_dt,
                    "confidence": row["confidence"],
                    "reasoning": row.get("reasoning", ""),
                    "event_title": row.get("event_title", ""),
                    "cached_at": cached_at,
                    "milestones_checked_at": None,  # Force re-check on restart
                }
                loaded += 1
            except (KeyError, ValueError) as e:
                logger.warning(
                    f"Skipping invalid DB estimate for {row.get('event_ticker')}: {e}",
                    exc_info=True,
                )

        if loaded:
            logger.info(f"Loaded {loaded} event estimates from database")

    def get(self, event_ticker: str) -> Optional[Dict[str, Any]]:
        """Get cached estimate. Once estimated, permanent until cleanup."""
        return self._cache.get(event_ticker)

    def put(self, event_ticker: str, data: Dict[str, Any]):
        """Cache an event estimate in memory and persist to DB."""
        now = datetime.now(timezone.utc)
        data["cached_at"] = now
        data.setdefault("milestones_checked_at", None)
        self._cache[event_ticker] = data

        # Persist to DB
        estimated_start = data.get("estimated_start_utc")
        if estimated_start:
            self._db.save_event_estimate(
                event_ticker=event_ticker,
                estimated_start_utc=estimated_start.isoformat(),
                confidence=data.get("confidence", "low"),
                reasoning=data.get("reasoning", ""),
                event_title=data.get("event_title", ""),
                cached_at=now.isoformat(),
            )

    def __len__(self):
        return len(self._cache)

    def get_stale_milestones(self, max_age_minutes: int = 30) -> List[str]:
        """Return event tickers whose milestones haven't been checked recently."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        return [
            et for et, data in self._cache.items()
            if data.get("milestones_checked_at") is None
            or data["milestones_checked_at"] < cutoff
        ]

    def update_milestones_checked(self, event_ticker: str):
        """Mark an event's milestones as just checked (without changing start time)."""
        entry = self._cache.get(event_ticker)
        if entry:
            entry["milestones_checked_at"] = datetime.now(timezone.utc)


class OrderGuard:
    """Monitors resting orders and cancels them before events start.

    Guards orders for specific strategies (configured via YAML guard: true),
    or all strategies if none specified.
    """

    def __init__(
        self,
        client: "KalshiClient",
        db: "Database",
        grok_api_key: str,
        cancel_buffer_minutes: int = 5,
        min_confidence: str = "medium",
        dry_run: bool = False,
        strategies: Optional[List[str]] = None,
    ):
        self.client = client
        self.db = db
        self.grok_api_key = grok_api_key
        self.cancel_buffer_minutes = cancel_buffer_minutes
        self.min_confidence = min_confidence
        self.min_confidence_level = CONFIDENCE_LEVELS.get(min_confidence, 2)
        self.dry_run = dry_run
        self.strategies = strategies
        self.event_cache = EventCache(db)
        self._market_cache: Dict[str, Dict[str, Any]] = {}
        self._cycle_errors = 0

        # Clean up estimates for events that started >24h ago
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cleaned = db.cleanup_old_estimates(cutoff)
        if cleaned:
            logger.info(f"Cleaned up {cleaned} old event estimates")

    @property
    def _strategy_label(self) -> str:
        """Label for log messages, e.g. ' [mentions]' or ' [mentions, longshot]' or ' [all]'."""
        if self.strategies:
            return f" [{', '.join(self.strategies)}]"
        return " [all]"

    def warm_cache(self, tickers: List[str]) -> Dict[str, int]:
        """Pre-fetch event start times for portfolio positions.

        Fetches market data for each ticker, groups by event, and queries
        Grok for any events not already in the cache.

        Args:
            tickers: Market tickers from portfolio positions.

        Returns:
            Dict with stats: tickers, events_found, events_cached, events_queried, errors.
        """
        stats = {
            "tickers": len(tickers),
            "events_found": 0,
            "events_cached": 0,
            "events_queried": 0,
            "milestones_resolved": 0,
            "grok_queried": 0,
            "errors": 0,
        }
        self._cycle_errors = 0

        # Fetch market data for each ticker, group by event
        events: Dict[str, List[Dict[str, Any]]] = {}
        for ticker in tickers:
            if ticker in self._market_cache:
                market = self._market_cache[ticker]
            else:
                try:
                    response = self.client.get_market(ticker)
                    market = response.get("market", {})
                    self._market_cache[ticker] = market
                except Exception as e:
                    logger.warning(f"Could not fetch market {ticker}: {e}", exc_info=True)
                    self._cycle_errors += 1
                    continue

            event_ticker = market.get("event_ticker", "")
            if not event_ticker:
                continue

            if event_ticker not in events:
                events[event_ticker] = []
            # Build a minimal order-like dict so _query_start_times can read ticker
            events[event_ticker].append({"ticker": ticker})

        stats["events_found"] = len(events)

        # Filter to uncached events
        uncached = [et for et in events if self.event_cache.get(et) is None]
        stats["events_cached"] = len(events) - len(uncached)

        if uncached:
            logger.info(f"Warming cache: {len(uncached)} uncached events")
            fallback = self._query_milestones(uncached, events)
            milestones_resolved = len(uncached) - len(fallback)
            stats["milestones_resolved"] = milestones_resolved
            if fallback:
                logger.info(f"Warming cache: querying Grok for {len(fallback)} events without milestones")
                self._query_start_times(fallback, events)
                stats["grok_queried"] = len(fallback)
            stats["events_queried"] = len(uncached)
        else:
            logger.info("Warming cache: all events already cached")

        stats["errors"] = self._cycle_errors
        return stats

    def run_cycle(self) -> Dict[str, int]:
        """Run one guard cycle: fetch orders, estimate times, cancel if needed.

        Returns:
            Dict with stats: resting_orders, events_checked, orders_cancelled, errors
        """
        stats = {
            "resting_orders": 0,
            "events_checked": 0,
            "milestones_refreshed": 0,
            "orders_cancelled": 0,
            "filled_contracts": 0,
            "errors": 0,
        }

        # Clear market cache each cycle to avoid serving stale data
        self._market_cache.clear()
        self._cycle_errors = 0

        # Refresh stale milestone data (every ~30min per event)
        refreshed = self._refresh_milestones()
        stats["milestones_refreshed"] = refreshed
        if refreshed:
            logger.info(f"Milestone refresh: {refreshed} events updated")

        # 1. Fetch resting orders, filtered to guarded strategies
        orders = self._fetch_guarded_orders()
        stats["resting_orders"] = len(orders)

        if not orders:
            logger.info(f"No resting guarded orders found{self._strategy_label}")
            return stats

        # 2. Enrich with market data and group by event
        events = self._enrich_and_group(orders)
        logger.info(f"Orders span {len(events)} events")

        # 3. Get start time estimates for uncached events
        uncached_tickers = [
            et for et in events if self.event_cache.get(et) is None
        ]

        if uncached_tickers:
            fallback = self._query_milestones(uncached_tickers, events)
            if fallback:
                logger.info(f"Querying Grok for {len(fallback)} events without milestones")
                self._query_start_times(fallback, events)
            stats["events_checked"] = len(uncached_tickers)

        # 4. Check each event and cancel if needed
        now = datetime.now(timezone.utc)
        for event_ticker, event_orders in events.items():
            cached = self.event_cache.get(event_ticker)
            if not cached:
                logger.warning(
                    f"No estimate for event {event_ticker} after milestones + Grok — "
                    f"CANCELLING {len(event_orders)} orders as precaution"
                )
                cancelled, filled = self._cancel_orders(event_orders)
                stats["orders_cancelled"] += cancelled
                stats["filled_contracts"] += filled
                continue

            estimated_start = cached.get("estimated_start_utc")
            confidence = cached.get("confidence", "low")
            confidence_level = CONFIDENCE_LEVELS.get(confidence, 1)

            if not estimated_start:
                continue

            minutes_until = (estimated_start - now).total_seconds() / 60
            logger.info(
                f"Event {event_ticker}: estimated start {estimated_start.strftime('%Y-%m-%d %H:%M UTC')} "
                f"({minutes_until:.0f}min from now), "
                f"confidence={confidence}, "
                f"{len(event_orders)} orders"
            )

            # Skip if confidence too low (but always cancel if event already started)
            if confidence_level < self.min_confidence_level and minutes_until > 0:
                logger.info(
                    f"  Skipping: confidence {confidence} < min {self.min_confidence}"
                )
                continue

            # Cancel if event starts within buffer (or already started)
            if minutes_until <= self.cancel_buffer_minutes:
                logger.warning(
                    f"  CANCELLING {len(event_orders)} orders: "
                    f"event starts in {minutes_until:.0f}min "
                    f"(<= {self.cancel_buffer_minutes}min buffer)"
                )
                cancelled, filled = self._cancel_orders(event_orders)
                stats["orders_cancelled"] += cancelled
                stats["filled_contracts"] += filled
            else:
                logger.info(
                    f"  Safe: {minutes_until:.0f}min until start "
                    f"(buffer={self.cancel_buffer_minutes}min)"
                )

        stats["errors"] = self._cycle_errors
        return stats

    def _fetch_guarded_orders(self) -> List[Dict[str, Any]]:
        """Fetch resting orders that belong to guarded strategies.

        Cross-references Kalshi's resting orders with our database to only
        return orders placed by the guarded strategies.
        """
        # Get order IDs from our database for guarded strategies
        guarded_order_ids: set = set()
        if self.strategies:
            for strategy in self.strategies:
                guarded_order_ids |= self.db.get_pending_order_ids(strategy=strategy)
        else:
            guarded_order_ids = self.db.get_pending_order_ids(strategy=None)

        if not guarded_order_ids:
            logger.info(f"No pending guarded orders in database{self._strategy_label}")
            return []

        logger.debug(f"Database has {len(guarded_order_ids)} pending guarded order IDs{self._strategy_label}")

        # Fetch all resting orders from Kalshi
        all_resting = []
        cursor = None

        while True:
            try:
                response = self.client.get_orders(
                    status="resting", limit=100, cursor=cursor
                )
            except Exception as e:
                logger.error(f"Error fetching orders: {e}", exc_info=True)
                self._cycle_errors += 1
                break

            orders = response.get("orders", [])
            all_resting.extend(orders)

            cursor = response.get("cursor")
            if not cursor or not orders:
                break

        # Filter to only guarded strategy orders
        guarded_orders = [
            o for o in all_resting if o["order_id"] in guarded_order_ids
        ]

        logger.info(
            f"Found {len(guarded_orders)} resting guarded orders{self._strategy_label} "
            f"(of {len(all_resting)} total resting)"
        )

        return guarded_orders

    def _enrich_and_group(
        self, orders: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Enrich orders with market data and group by event_ticker."""
        events: Dict[str, List[Dict[str, Any]]] = {}

        # Get unique tickers
        tickers = set(o["ticker"] for o in orders)

        # Fetch market data for each ticker (cached)
        for ticker in tickers:
            if ticker not in self._market_cache:
                try:
                    response = self.client.get_market(ticker)
                    self._market_cache[ticker] = response.get("market", {})
                except Exception as e:
                    logger.error(f"Error fetching market {ticker}: {e}", exc_info=True)
                    self._cycle_errors += 1

        # Group orders by event
        for order in orders:
            ticker = order["ticker"]
            market = self._market_cache.get(ticker, {})
            event_ticker = market.get("event_ticker", "")

            if not event_ticker:
                logger.warning(f"No event_ticker for market {ticker}, skipping")
                continue

            if event_ticker not in events:
                events[event_ticker] = []
            events[event_ticker].append(order)

        return events

    @staticmethod
    def _select_best_milestone(milestones: List[Dict[str, Any]]) -> Optional[datetime]:
        """Pick the best start time from a list of milestones.

        Selection priority:
        1. Prefer category == "mentions" with start_date
        2. Otherwise closest future start_date
        3. If all past, use the most recent one

        Returns:
            Parsed UTC datetime, or None if no valid start_date found.
        """
        now = datetime.now(timezone.utc)

        selected = None
        mentions_milestones = [
            m for m in milestones
            if m.get("category") == "mentions" and m.get("start_date")
        ]
        if mentions_milestones:
            selected = mentions_milestones[0]
        else:
            dated = [m for m in milestones if m.get("start_date")]
            if dated:
                future = []
                for m in dated:
                    try:
                        dt = datetime.fromisoformat(
                            m["start_date"].replace("Z", "+00:00")
                        )
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt > now:
                            future.append((dt, m))
                    except (ValueError, KeyError):
                        continue
                if future:
                    future.sort(key=lambda x: x[0])
                    selected = future[0][1]
                else:
                    selected = dated[0]

        if not selected or not selected.get("start_date"):
            return None

        start_str = selected["start_date"]
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        return start_dt

    def _query_milestones(
        self,
        event_tickers: List[str],
        events: Dict[str, List[Dict[str, Any]]],
    ) -> List[str]:
        """Query Kalshi Milestones API for event start times.

        Args:
            event_tickers: Event tickers to look up.
            events: Mapping of event_ticker -> list of orders (for context).

        Returns:
            List of event tickers that need Grok fallback (no milestone data).
        """
        fallback_tickers = []
        now = datetime.now(timezone.utc)

        for et in event_tickers:
            try:
                response = self.client.get_milestones(et)
                milestones = response.get("milestones", [])

                if not milestones:
                    logger.info(f"[milestones] {et}: no milestone data, will query Grok")
                    fallback_tickers.append(et)
                    continue

                start_dt = self._select_best_milestone(milestones)

                if not start_dt:
                    logger.info(f"[milestones] {et}: no start_date in milestones, will query Grok")
                    fallback_tickers.append(et)
                    continue

                # Fetch event title for cache
                event_title = ""
                try:
                    event_response = self.client.get_event(et)
                    event_title = event_response.get("event", {}).get("title", "")
                except Exception:
                    pass

                self.event_cache.put(
                    et,
                    {
                        "estimated_start_utc": start_dt,
                        "confidence": "high",
                        "reasoning": "Kalshi milestones API",
                        "event_title": event_title,
                        "milestones_checked_at": now,
                    },
                )
                logger.info(f"[milestones] {et}: start_date {start_dt.isoformat()}")

            except Exception as e:
                logger.warning(f"[milestones] {et}: API error ({e}), will query Grok", exc_info=True)
                fallback_tickers.append(et)

        return fallback_tickers

    def _query_start_times(
        self,
        event_tickers: List[str],
        events: Dict[str, List[Dict[str, Any]]],
    ):
        """Query Grok for estimated start times using tool use with web + X search."""
        # Build rich event info, fetching event-level data from API
        event_info_map: Dict[str, Dict[str, Any]] = {}
        for et in event_tickers:
            order_list = events.get(et, [])
            if not order_list:
                continue

            # Fetch event-level data (title, subtitle, category)
            event_data = {}
            try:
                response = self.client.get_event(et)
                event_data = response.get("event", {})
            except Exception as e:
                logger.warning(f"Could not fetch event {et}: {e}", exc_info=True)

            # Collect all market titles for this event from _market_cache
            market_titles = []
            close_time = ""
            for order in order_list:
                market = self._market_cache.get(order["ticker"], {})
                title = market.get("title", "")
                if title and title not in market_titles:
                    market_titles.append(title)
                if not close_time:
                    close_time = market.get("close_time", "")

            event_info_map[et] = {
                "event_ticker": et,
                "event_title": event_data.get("title", ""),
                "subtitle": event_data.get("subtitle", ""),
                "category": event_data.get("category", ""),
                "market_titles": market_titles,
                "close_time": close_time,
            }

        # Batch up to 10 events per Grok call
        ticker_list = [et for et in event_tickers if et in event_info_map]
        for i in range(0, len(ticker_list), 10):
            batch = ticker_list[i : i + 10]
            events_info = [event_info_map[et] for et in batch]

            if not events_info:
                continue

            now = datetime.now(timezone.utc)
            now_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            today_str = now.strftime("%B %d, %Y")
            user_message = (
                f"Today's date: {today_str}\n"
                f"Current UTC time: {now_utc}\n\n"
                f"Estimate start times for these events. "
                f"Find only the NEXT future instance of each event:\n"
                f"{json.dumps(events_info, indent=2)}"
            )

            logger.debug(f"Grok request:\n{user_message}")

            try:
                payload = {
                    "model": GROK_MODEL,
                    "instructions": SYSTEM_PROMPT,
                    "input": [
                        {"role": "user", "content": user_message},
                    ],
                    "tools": GROK_TOOLS,
                    "tool_choice": "required",
                }

                resp = http_requests.post(
                    GROK_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.grok_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=120,
                )
                if resp.status_code >= 400:
                    logger.error(f"Grok API error {resp.status_code}: {resp.text[:1000]}")
                resp.raise_for_status()
                data = resp.json()

                # Log usage
                usage = data.get("usage", {})
                if usage:
                    logger.info(
                        f"Grok usage: {usage.get('input_tokens', '?')} in / "
                        f"{usage.get('output_tokens', '?')} out"
                    )

                # Extract function_call items from the output array
                output_items = data.get("output", [])

                # Find our report_event_start_times call
                tool_call = None
                for item in output_items:
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") == "report_event_start_times"
                    ):
                        tool_call = item
                        break

                if not tool_call:
                    logger.error("Grok did not call report_event_start_times")
                    # Log what we got for debugging
                    for item in output_items:
                        if item.get("type") == "message":
                            for c in item.get("content", []):
                                if c.get("type") == "output_text":
                                    logger.debug(f"Grok text response: {c.get('text', '')[:500]}")
                    self._cycle_errors += 1
                    continue

                arguments_str = tool_call.get("arguments", "{}")
                arguments = json.loads(arguments_str)
                estimates = arguments.get("events", [])
                logger.debug(f"Grok tool response: {json.dumps(estimates, indent=2)}")

                for est in estimates:
                    et = est.get("event_ticker")
                    if not et:
                        continue

                    try:
                        start_str = est["estimated_start_utc"]
                        start_dt = datetime.fromisoformat(
                            start_str.replace("Z", "+00:00")
                        )
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=timezone.utc)

                        event_title = event_info_map.get(et, {}).get(
                            "event_title", ""
                        )
                        self.event_cache.put(
                            et,
                            {
                                "estimated_start_utc": start_dt,
                                "confidence": est.get("confidence", "low"),
                                "reasoning": est.get("reasoning", ""),
                                "event_title": event_title,
                            },
                        )
                        logger.info(
                            f"  {et}: estimated start {start_dt.isoformat()}, "
                            f"confidence={est.get('confidence', 'low')}, "
                            f"reason={est.get('reasoning', '')}"
                        )
                    except (KeyError, ValueError) as e:
                        logger.error(
                            f"Error parsing estimate for {et}: {e}",
                            exc_info=True,
                        )
                        self._cycle_errors += 1

            except Exception as e:
                logger.error(f"Error querying Grok: {e}", exc_info=True)
                self._cycle_errors += 1

    def _refresh_milestones(self, max_per_cycle: int = 5) -> int:
        """Re-check milestones for cached events to catch schedule changes.

        Only re-queries events whose milestones haven't been checked in 30 minutes.
        If milestones returns updated data, refreshes the cache.
        If milestones returns empty or errors, keeps the existing cached time.

        Args:
            max_per_cycle: Cap on API calls per cycle to avoid burst after restart.

        Returns:
            Number of events whose start time was actually updated.
        """
        stale = self.event_cache.get_stale_milestones(max_age_minutes=30)
        if not stale:
            return 0

        if len(stale) > max_per_cycle:
            logger.info(
                f"Milestone refresh: {len(stale)} stale events, "
                f"capping to {max_per_cycle} this cycle"
            )
            stale = stale[:max_per_cycle]

        logger.info(f"Milestone refresh: checking {len(stale)} events")
        updated = 0

        for et in stale:
            try:
                response = self.client.get_milestones(et)
                milestones = response.get("milestones", [])

                if not milestones:
                    # No data — keep existing estimate, mark as checked
                    self.event_cache.update_milestones_checked(et)
                    continue

                start_dt = self._select_best_milestone(milestones)

                if not start_dt:
                    # No valid start_date — keep existing, mark as checked
                    self.event_cache.update_milestones_checked(et)
                    continue

                # Compare with existing cached time
                existing = self.event_cache.get(et)
                old_start = existing.get("estimated_start_utc") if existing else None

                if old_start == start_dt:
                    # Same time — just mark as checked, skip DB write
                    self.event_cache.update_milestones_checked(et)
                    continue

                # Time actually changed — update cache + persist to DB
                logger.warning(
                    f"[milestone refresh] {et}: start time changed "
                    f"{old_start.isoformat() if old_start else 'None'} -> {start_dt.isoformat()}"
                )
                updated += 1
                now = datetime.now(timezone.utc)
                self.event_cache.put(
                    et,
                    {
                        "estimated_start_utc": start_dt,
                        "confidence": "high",
                        "reasoning": "Kalshi milestones API (refreshed)",
                        "event_title": existing.get("event_title", "") if existing else "",
                        "milestones_checked_at": now,
                    },
                )

            except Exception as e:
                # API error — mark as checked so we don't hammer every cycle
                logger.warning(f"[milestone refresh] {et}: API error ({e}), keeping existing estimate", exc_info=True)
                self.event_cache.update_milestones_checked(et)

        return updated

    def _cancel_orders(self, orders: List[Dict[str, Any]]) -> tuple:
        """Cancel orders, using batch API in groups of 20. Track partial fills.

        Args:
            orders: List of order dicts (must have 'order_id', may have 'initial_count')

        Returns:
            Tuple of (cancelled_count, total_filled_contracts)
        """
        cancelled = 0
        total_filled = 0

        if self.dry_run:
            for order in orders:
                logger.info(f"  [DRY RUN] Would cancel order {order['order_id']}")
            return len(orders), 0

        order_ids = [o["order_id"] for o in orders]
        # Build lookup for initial_count from the order objects
        initial_counts = {
            o["order_id"]: o.get("initial_count", 0) for o in orders
        }

        # Batch cancel in groups of 20
        for i in range(0, len(order_ids), 20):
            batch = order_ids[i : i + 20]

            try:
                self.client.batch_cancel_orders(batch)
                # Batch cancel doesn't return per-order fill data;
                # fetch each order to get final fill_count
                for oid in batch:
                    fill_count = self._get_fill_count(oid, initial_counts.get(oid, 0))
                    self.db.update_order_status(oid, "cancelled", filled_quantity=fill_count)
                    total_filled += fill_count or 0
                    cancelled += 1
                logger.info(f"  Batch cancelled {len(batch)} orders")
            except Exception as e:
                logger.warning(
                    f"  Batch cancel failed ({e}), falling back to individual",
                    exc_info=True,
                )
                for oid in batch:
                    try:
                        response = self.client.cancel_order(oid)
                        # cancel_order returns the final order state
                        order_data = response.get("order", {})
                        fill_count = order_data.get("fill_count", 0)
                        initial = order_data.get("initial_count", initial_counts.get(oid, 0))
                        self.db.update_order_status(oid, "cancelled", filled_quantity=fill_count)
                        total_filled += fill_count
                        cancelled += 1
                        remaining = initial - fill_count if initial else "?"
                        logger.info(
                            f"  Cancelled order {oid}: "
                            f"{fill_count}/{initial} contracts filled, "
                            f"{remaining} cancelled"
                        )
                    except Exception as e2:
                        logger.error(f"  Failed to cancel order {oid}: {e2}", exc_info=True)
                        self._cycle_errors += 1

        return cancelled, total_filled

    def _get_fill_count(self, order_id: str, initial_count: int) -> Optional[int]:
        """Fetch the final fill_count for a cancelled order.

        Args:
            order_id: Kalshi order ID
            initial_count: Original order quantity for logging

        Returns:
            Number of contracts filled, or None if unknown (API error).
            None preserves legacy semantics so the order stays in the
            settlement queue rather than being excluded as zero-fill.
        """
        try:
            response = self.client.get_order(order_id)
            order_data = response.get("order", {})
            fill_count = order_data.get("fill_count", 0)
            initial = order_data.get("initial_count", initial_count)
            remaining = initial - fill_count if initial else "?"
            logger.info(
                f"  Cancelled order {order_id}: "
                f"{fill_count}/{initial} contracts filled, "
                f"{remaining} cancelled"
            )
            return fill_count
        except Exception as e:
            logger.warning(f"  Could not fetch fill count for {order_id}: {e}", exc_info=True)
            return None
