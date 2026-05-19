# src/runner.py
"""Continuous runner — unified tick loop for trading, guard, and settlement."""
import logging
import queue
import time
from datetime import datetime, timedelta
from typing import Any, Optional, TYPE_CHECKING

from datetime import timezone as tz

from src.strategies import discover_guarded_strategies, discover_strategies
from typing import Dict, List

if TYPE_CHECKING:
    from src.config import Config
    from src.database import Database
    from src.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)

# Minutes past the hour when trading + settlement run
TRADE_MINUTES = (0, 30)

# Tick interval in minutes
TICK_INTERVAL = 5


class Runner:
    """Continuous tick loop orchestrating trading, guard, reprice, and settlement.

    Schedule:
    - Every tick: guard runs FIRST (populates event cache for safety checks)
    - At :00 and :30 past every hour: guard -> reprice -> trade -> settle
    """

    def __init__(
        self,
        config: "Config",
        client: "KalshiClient",
        db: "Database",
        dry_run: bool = False,
        strategies: List[str] | None = None,
    ):
        self.config = config
        self.client = client
        self.db = db
        self.dry_run = dry_run
        self.strategies = strategies
        self._running = False

        # Components (lazy-initialized)
        self._guard = None
        self._ws = None
        self._reconciler = None

        # Thread-safe queues for WS events (processed on main thread)
        self._fill_queue: queue.Queue = queue.Queue()
        self._settlement_queue: queue.Queue = queue.Queue()
        import threading
        self._needs_reconciliation = threading.Event()  # Set by WS reconnect callback

    def start(self):
        """Run the continuous loop: reconcile -> connect WS -> tick loop.

        Blocks until stopped via Ctrl+C.
        """
        self._running = True

        print()
        print(f"Kalshi Continuous Runner - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 50)
        if self.dry_run:
            print("[DRY RUN MODE]")
        if self.strategies:
            print(f"Strategies: {', '.join(self.strategies)}")
        print()

        logger.info(
            f"Runner starting: dry_run={self.dry_run}, "
            f"strategies={self.strategies or 'all (discover)'}"
        )

        # 1. Reconcile
        try:
            self._reconcile()
        except Exception:
            logger.exception("Reconciliation failed — continuing without")

        # 2. Connect WebSocket
        self._start_websocket()

        # 3. Initialize guard
        try:
            self._init_guard()
        except Exception:
            logger.exception("Guard init failed — continuing without guard")

        # 3b. Warm guard cache with portfolio positions
        try:
            self._warm_guard_cache()
        except Exception:
            logger.exception("Guard cache warm failed — continuing")

        # 4. Enter tick loop
        print()
        print("Entering tick loop (Ctrl+C to stop)")
        print(f"  Guard: every {TICK_INTERVAL} minutes")
        print(f"  Trade + Settle: at :{TRADE_MINUTES[0]:02d} and :{TRADE_MINUTES[1]:02d}")
        print(f"  Reprice: at trade ticks (stale orders after 6h)")
        print()

        try:
            self._tick_loop()
        except KeyboardInterrupt:
            print()
            print("Shutting down...")
        finally:
            self.stop()

    def stop(self):
        """Clean shutdown."""
        self._running = False
        if self._ws:
            self._ws.stop()
        self.db.close()
        logger.info("Runner stopped")

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _reconcile(self):
        """Run startup reconciliation."""
        from src.reconciler import Reconciler

        self._reconciler = Reconciler(self.client, self.db)

        print("Reconciling orders...")
        order_stats = self._reconciler.reconcile_orders()
        print(
            f"  {order_stats['updated']} updated, "
            f"{order_stats['unchanged']} unchanged, "
            f"{order_stats['missing']} missing"
        )
        logger.info(f"Order reconciliation: {order_stats}")

        print("Reconciling settlements...")
        settle_stats = self._reconciler.reconcile_settlements()
        print(
            f"  {settle_stats['updated']} settled, "
            f"{settle_stats['still_pending']} pending"
        )
        logger.info(f"Settlement reconciliation: {settle_stats}")

    def _init_guard(self):
        """Initialize the order guard for YAML-opted strategies."""
        from src.order_guard import OrderGuard

        if not self.config.grok_api_key:
            logger.warning("No XAI_API_KEY — guard disabled")
            print("  Warning: guard disabled (no XAI_API_KEY)")
            return

        guarded = discover_guarded_strategies()
        if not guarded:
            logger.info("No strategies have guard: true — guard disabled")
            print("  Guard: no strategies opted in (guard: true)")
            return

        logger.info(f"Guard enabled for strategies: {guarded}")
        print(f"  Guard: enabled for {guarded}")

        self._guard = OrderGuard(
            client=self.client,
            db=self.db,
            grok_api_key=self.config.grok_api_key,
            cancel_buffer_minutes=60,
            min_confidence="medium",
            dry_run=self.dry_run,
            strategies=guarded,
        )

    def _warm_guard_cache(self):
        """Pre-fetch event start times for all held portfolio positions.

        This fills the guard's event cache so that new resting orders on
        markets where we already hold positions benefit from cached start
        times immediately, without waiting for the next Grok query.
        """
        if not self._guard:
            return

        # Fetch all positions from Kalshi
        tickers = []
        cursor = None
        while True:
            try:
                result = self.client.get_positions(
                    limit=100, cursor=cursor, count_filter="position"
                )
            except Exception as e:
                logger.error(f"Error fetching positions for cache warm: {e}", exc_info=True)
                break

            for pos in result.get("market_positions", []):
                ticker = pos.get("ticker", "")
                if ticker:
                    tickers.append(ticker)

            cursor = result.get("cursor")
            if not cursor or not result.get("market_positions"):
                break

        if not tickers:
            logger.info("No portfolio positions to warm cache with")
            return

        logger.info(f"Warming guard cache with {len(tickers)} position tickers")
        for ticker in tickers:
            logger.debug(f"  Position: {ticker}")

        stats = self._guard.warm_cache(tickers)
        milestones = stats.get('milestones_resolved', 0)
        grok = stats.get('grok_queried', 0)
        print(
            f"  Guard cache: {stats['events_found']} events "
            f"({stats['events_cached']} cached, "
            f"{milestones} via milestones, {grok} via Grok)"
        )

    def _start_websocket(self):
        """Connect WebSocket for real-time fills and settlements."""
        try:
            from src.ws_client import KalshiWebSocket

            # Derive WS URL from REST base URL
            rest_url = self.config.api_base_url
            ws_url = rest_url.replace(
                "https://", "wss://"
            ).replace(
                "/trade-api/v2", "/trade-api/ws/v2"
            )

            self._ws = KalshiWebSocket(
                client=self.client,
                on_fill=self._on_ws_fill,
                on_settlement=self._on_ws_settlement,
                on_reconnect=self._on_ws_reconnect,
                base_ws_url=ws_url,
            )
            self._ws.start()
            print(f"  WebSocket: connecting to {ws_url}")
        except ImportError:
            logger.warning("websocket-client not installed — REST-only mode", exc_info=True)
            print("  WebSocket: unavailable (websocket-client not installed)")
        except Exception as e:
            logger.warning(f"WebSocket failed: {e} — REST-only mode", exc_info=True)
            print(f"  WebSocket: failed ({e})")

    # ------------------------------------------------------------------
    # WebSocket callbacks (run on WS thread, queue for main thread)
    # ------------------------------------------------------------------

    def _on_ws_fill(self, order_id: str, fill_data: dict):
        """WS fill callback — queue for main thread processing."""
        self._fill_queue.put((order_id, fill_data))

    def _on_ws_settlement(self, market_ticker: str, result: str):
        """WS settlement callback — queue for main thread processing."""
        self._settlement_queue.put((market_ticker, result))

    def _on_ws_reconnect(self):
        """WS reconnect callback — flag for main thread reconciliation."""
        logger.info("WebSocket reconnected — flagging reconciliation")
        self._needs_reconciliation.set()

    def _process_ws_events(self):
        """Process queued WS events on the main thread (DB writes)."""
        # Mini-reconciliation after WS reconnect
        if self._needs_reconciliation.is_set() and self._reconciler:
            self._needs_reconciliation.clear()
            logger.info("Running mini-reconciliation after WS reconnect")
            self._reconciler.reconcile_orders()
            self._reconciler.reconcile_settlements()

        fills_processed = 0
        while True:
            try:
                order_id, _ = self._fill_queue.get_nowait()
            except queue.Empty:
                break
            try:
                response = self.client.get_order(order_id)
                order_data = response.get("order", {})
                api_status = order_data.get("status", "")
                fill_count = order_data.get("fill_count", 0)
                initial_count = order_data.get("initial_count", fill_count)

                if api_status == "executed":
                    self.db.update_order_status(
                        order_id, "filled", filled_quantity=initial_count
                    )
                    logger.info(
                        f"Fill recorded: {order_id} fully filled "
                        f"({initial_count} contracts)"
                    )
                else:
                    logger.info(
                        f"Partial fill: {order_id} has {fill_count}/{initial_count} "
                        f"filled (status={api_status})"
                    )
                fills_processed += 1
            except Exception as e:
                logger.error(f"Error processing WS fill for {order_id}: {e}", exc_info=True)

        # Process settlement events — trigger immediate settlement check
        settlements_received = 0
        while not self._settlement_queue.empty():
            try:
                self._settlement_queue.get_nowait()
                settlements_received += 1
            except queue.Empty:
                break

        if settlements_received:
            logger.info(f"WS: {settlements_received} settlement events — running settlement check")
            self._run_settlements(datetime.now().strftime("%H:%M:%S"))

        if fills_processed:
            logger.info(f"Processed {fills_processed} WebSocket fill events")

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    def _tick_loop(self):
        """Main loop: sleep until next 5-min boundary, run tick."""
        while self._running:
            sleep_secs = self._seconds_until_next_tick()
            if sleep_secs > 0:
                next_time = datetime.now() + timedelta(seconds=sleep_secs)
                logger.info(f"Next tick at {next_time.strftime('%H:%M')}")

                # Sleep in 1-second intervals so we can respond to shutdown
                end_time = time.time() + sleep_secs
                while time.time() < end_time and self._running:
                    time.sleep(min(1.0, end_time - time.time()))

                if not self._running:
                    break

            try:
                self._tick()
            except Exception:
                logger.exception("Tick failed — will retry next cycle")

    def _tick(self):
        """Execute one tick."""
        now = datetime.now()
        minute = now.minute
        tick_time = now.strftime("%H:%M:%S")
        is_trade_tick = minute in TRADE_MINUTES

        ws_status = "connected" if (self._ws and self._ws.connected) else "disconnected"
        logger.info(f"--- Tick {tick_time} (WS: {ws_status}) ---")

        # Process any WS events first
        self._process_ws_events()

        # Guard ALWAYS runs first — it populates the event cache that
        # trading and reprice rely on to skip started/imminent events.
        self._run_guard(tick_time)

        if is_trade_tick:
            self._run_reprice(tick_time)
            self._run_trading(tick_time)
            self._run_settlements(tick_time)

    # ------------------------------------------------------------------
    # Tick phases
    # ------------------------------------------------------------------

    def _run_trading(self, tick_time: str):
        """Run all discovered strategies to place new orders."""
        from src.order_placer import OrderPlacer

        logger.info(f"[{tick_time}] Running trading cycle...")

        strategy_names = self.strategies if self.strategies else discover_strategies()
        source = "explicit" if self.strategies else "discovered"
        logger.info(f"[{tick_time}] Trading cycle: strategies={strategy_names} ({source})")

        if not strategy_names:
            logger.info(f"[{tick_time}] No strategies discovered")
            print(f"[{tick_time}] Trading: no strategies found")
            return

        placer = OrderPlacer(client=self.client, dry_run=self.dry_run)
        committed = self._get_portfolio_commitment()
        total_successes = 0
        total_failures = 0

        for strategy_name in strategy_names:
            successes, failures = self._run_single_strategy(
                strategy_name, placer, tick_time, committed
            )
            total_successes += successes
            total_failures += failures

        dry_str = " [DRY]" if self.dry_run else ""
        print(
            f"[{tick_time}] Trading{dry_str}: "
            f"{total_successes} placed, {total_failures} failed"
        )
        logger.info(
            f"[{tick_time}] Trading cycle complete: "
            f"placed={total_successes}, failed={total_failures}, dry_run={self.dry_run}"
        )

    def _run_single_strategy(
        self, strategy_name: str, placer: Any, tick_time: str,
        committed: Dict[str, int],
    ) -> tuple:
        """Run a single strategy: load config, find markets, place orders.

        Returns:
            Tuple of (successes, failures)
        """
        from src.strategies import get_strategy_class, load_strategy_config

        try:
            strategy_config = load_strategy_config(f"strategies/{strategy_name}.yaml")
        except FileNotFoundError:
            logger.warning(f"strategies/{strategy_name}.yaml not found, skipping", exc_info=True)
            return 0, 0

        try:
            StrategyClass = get_strategy_class(strategy_name)
            strategy = StrategyClass(self.client, strategy_config)
        except Exception as e:
            logger.error(f"Failed to load {strategy_name} strategy: {e}", exc_info=True)
            return 0, 0

        try:
            markets = strategy.find_markets()
            logger.info(f"  [{strategy_name}] Market scan: {len(markets)} qualifying markets")
        except Exception as e:
            logger.error(f"Error scanning markets for {strategy_name}: {e}", exc_info=True)
            return 0, 0

        # Skip tickers we've already placed orders on (DB-based dedup)
        traded = self.db.get_traded_tickers(strategy_name, pending_only=True)
        if markets and traded:
            original = len(markets)
            markets = [m for m in markets if m["ticker"] not in traded]
            skipped = original - len(markets)
            if skipped > 0:
                logger.info(f"  [{strategy_name}] Skipped {skipped} already-traded tickers")

        # Safety net: also skip markets at contract cap from live portfolio
        contract_cap = strategy_config.get("order", {}).get("contracts_per_market", 500)
        if markets and committed:
            original = len(markets)
            markets = [m for m in markets if committed.get(m["ticker"], 0) < contract_cap]
            skipped = original - len(markets)
            if skipped > 0:
                logger.info(f"  [{strategy_name}] Skipped {skipped} markets at contract cap ({contract_cap})")

        if not markets:
            logger.info(f"[{tick_time}] {strategy_name}: no new markets")
            return 0, 0

        logger.info(f"  [{strategy_name}] Placing orders on {len(markets)} markets")

        successes = 0
        failures = 0

        for market in markets:
            # Skip markets whose events have started or are within guard buffer
            if self._guard:
                event_ticker = market.get("event_ticker", "")
                if event_ticker:
                    if not self._guard_check_event(
                        event_ticker, market.get("ticker", "?"), strategy_name
                    ):
                        continue

            order_list = strategy.calculate_orders(market)
            if not order_list:
                logger.debug(f"  [{strategy_name}] {market.get('ticker', '?')}: calculate_orders returned empty, skipping")
                continue

            for order_params in order_list:
                result = placer.place_order_from_params(order_params, strategy=strategy_name)

                if result.success:
                    successes += 1
                    logger.info(
                        f"  [{strategy_name}] Order placed: {order_params.ticker} "
                        f"{order_params.side.upper()} {order_params.quantity}x @ {order_params.price_cents}c "
                        f"(order_id={result.order_id}, dry_run={result.dry_run})"
                    )
                    if not result.dry_run:
                        self.db.record_order(
                            client_order_id=result.client_order_id,
                            order_id=result.order_id,
                            strategy=strategy_name,
                            ticker=order_params.ticker,
                            side=order_params.side,
                            action=order_params.action,
                            price_cents=order_params.price_cents,
                            quantity=order_params.quantity,
                            status="pending",
                            market_snapshot=order_params.market_snapshot,
                        )
                else:
                    failures += 1
                    logger.warning(
                        f"  [{strategy_name}] Order failed: {order_params.ticker} "
                        f"{order_params.side.upper()} {order_params.quantity}x @ {order_params.price_cents}c "
                        f"error={result.error}"
                    )

        logger.info(f"  [{strategy_name}] {successes} placed, {failures} failed")
        return successes, failures

    def _run_guard(self, tick_time: str):
        """Run the order guard."""
        if not self._guard:
            return

        logger.info(f"[{tick_time}] Running guard cycle...")
        stats = self._guard.run_cycle()

        filled = stats.get("filled_contracts", 0)
        filled_str = f" ({filled} already filled)" if filled else ""
        errors_str = f", errors: {stats['errors']}" if stats.get("errors") else ""

        print(
            f"[{tick_time}] Guard: "
            f"{stats['resting_orders']} resting, "
            f"{stats['orders_cancelled']} cancelled{filled_str}"
            f"{errors_str}"
        )
        logger.info(
            f"[{tick_time}] Guard cycle complete: resting={stats['resting_orders']}, "
            f"cancelled={stats['orders_cancelled']}, filled_contracts={filled}, "
            f"errors={stats.get('errors', 0)}"
        )

    def _run_settlements(self, tick_time: str):
        """Run settlement checking."""
        from src.settlement_checker import SettlementChecker

        logger.info(f"[{tick_time}] Running settlement check...")
        checker = SettlementChecker(self.client, self.db)
        results = checker.check_all()

        print(
            f"[{tick_time}] Settlements: "
            f"{results['updated']} settled, "
            f"{results['still_pending']} pending"
        )
        logger.info(
            f"[{tick_time}] Settlement check complete: "
            f"updated={results['updated']}, still_pending={results['still_pending']}, "
            f"errors={results.get('errors', 0)}"
        )

    # ------------------------------------------------------------------
    # Guard helpers
    # ------------------------------------------------------------------

    def _guard_check_event(
        self, event_ticker: str, ticker: str, strategy_name: str
    ) -> bool:
        """Check guard cache for event; query milestones if uncached.

        Returns True if the order is safe to place, False if it should be
        skipped (event imminent or unknown).
        """
        cached = self._guard.event_cache.get(event_ticker)

        # If not cached, query milestones API to populate the cache
        if not cached:
            logger.info(
                f"  [{strategy_name}] {ticker}: event {event_ticker} not in "
                f"guard cache, querying milestones..."
            )
            fallback = self._guard._query_milestones(
                [event_ticker], {event_ticker: [{"ticker": ticker}]}
            )
            if fallback:
                # Milestones had no data — try Grok as fallback
                logger.info(
                    f"  [{strategy_name}] {ticker}: no milestones, querying Grok..."
                )
                self._guard._query_start_times(
                    fallback, {event_ticker: [{"ticker": ticker}]}
                )
                cached = self._guard.event_cache.get(event_ticker)
                if not cached:
                    logger.warning(
                        f"  [{strategy_name}] {ticker}: no start time from "
                        f"milestones or Grok, blocking"
                    )
                    return False
            else:
                # Milestones succeeded — re-check cache
                cached = self._guard.event_cache.get(event_ticker)

        if cached and cached.get("estimated_start_utc"):
            now_utc = datetime.now(tz.utc)
            mins_until = (
                cached["estimated_start_utc"] - now_utc
            ).total_seconds() / 60
            if mins_until <= self._guard.cancel_buffer_minutes:
                logger.info(
                    f"  [{strategy_name}] {ticker}: event {event_ticker} "
                    f"starts in {mins_until:.0f}min, skipping"
                )
                return False

        return True

    # ------------------------------------------------------------------
    # Reprice
    # ------------------------------------------------------------------

    def _run_reprice(self, tick_time: str):
        """Cancel stale resting orders and re-place at current market prices.

        Groups resting mentions orders by ticker. If ANY order on a ticker
        is stale, cancels ALL orders on that ticker and re-places using the
        strategy's calculate_orders() (full ladder).
        """
        from src.order_placer import OrderPlacer
        from src.strategies import get_strategy_class, load_strategy_config

        # Load mentions config and check if reprice is enabled
        try:
            config = load_strategy_config("strategies/mentions.yaml")
        except FileNotFoundError:
            logger.debug("[reprice] mentions.yaml not found, skipping")
            return

        reprice_cfg = config.get("reprice", {})
        if not reprice_cfg.get("enabled", False):
            logger.debug("[reprice] disabled in config")
            return

        after_hours = reprice_cfg.get("after_hours", 6)

        # Get mentions order IDs from DB — exclude already-repriced orders
        mentions_order_ids = self.db.get_pending_order_ids(
            strategy="mentions", exclude_action="reprice"
        )
        if not mentions_order_ids:
            logger.info(f"[{tick_time}] [reprice] no pending mentions orders")
            return

        # Fetch all resting orders from Kalshi API
        resting_orders = []
        cursor = None
        while True:
            try:
                result = self.client.get_orders(
                    status="resting", limit=100, cursor=cursor
                )
            except Exception as e:
                logger.error(f"[reprice] Error fetching resting orders: {e}", exc_info=True)
                break

            for order in result.get("orders", []):
                if order.get("order_id") in mentions_order_ids:
                    resting_orders.append(order)

            cursor = result.get("cursor")
            if not cursor or not result.get("orders"):
                break

        if not resting_orders:
            logger.info(f"[{tick_time}] [reprice] no matching resting orders")
            return

        # Group resting orders by ticker
        by_ticker: Dict[str, List[dict]] = {}
        for order in resting_orders:
            ticker = order.get("ticker", "")
            by_ticker.setdefault(ticker, []).append(order)

        # Instantiate strategy for calculate_orders()
        try:
            StrategyClass = get_strategy_class("mentions")
            strategy = StrategyClass(self.client, config)
        except Exception as e:
            logger.error(f"[reprice] failed to load mentions strategy: {e}", exc_info=True)
            return

        placer = OrderPlacer(client=self.client, dry_run=self.dry_run)
        repriced = 0
        skipped = 0
        now_utc = datetime.now(tz.utc)

        for ticker, orders in by_ticker.items():
            # Check staleness on the oldest order in this ticker group
            oldest_created = None
            for order in orders:
                created_time = order.get("created_time", "")
                try:
                    created_dt = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
                    if oldest_created is None or created_dt < oldest_created:
                        oldest_created = created_dt
                except (ValueError, TypeError):
                    pass

            if oldest_created is None:
                logger.warning(f"[reprice] {ticker}: no valid created_time, skipping")
                skipped += len(orders)
                continue

            age_hours = (now_utc - oldest_created).total_seconds() / 3600
            if age_hours < after_hours:
                logger.debug(f"[reprice] {ticker}: oldest order {age_hours:.1f}h old < {after_hours}h, skipping")
                skipped += len(orders)
                continue

            # Fetch fresh market data
            try:
                market_data = self.client.get_market(ticker)
                market = market_data.get("market", market_data)
            except Exception as e:
                logger.warning(f"[reprice] failed to fetch {ticker}: {e}", exc_info=True)
                skipped += len(orders)
                continue

            # Normalize API price fields
            from src.market_utils import normalize_market_prices
            normalize_market_prices([market])

            # Check market is still open/active
            if market.get("status") not in ("open", "active"):
                logger.debug(f"[reprice] {ticker}: not open (status={market.get('status')}), skipping")
                skipped += len(orders)
                continue

            # Check guard — skip if event imminent or unknown
            if self._guard:
                event_ticker = market.get("event_ticker", "")
                if event_ticker and not self._guard_check_event(
                    event_ticker, ticker, "reprice"
                ):
                    skipped += len(orders)
                    continue

            no_bid = market.get("no_bid")
            if no_bid is None or no_bid <= 0:
                logger.debug(f"[reprice] {ticker}: no liquidity (no_bid={no_bid})")
                skipped += len(orders)
                continue

            # Normalize no_bid to cents for _no_bid field
            if no_bid <= 1:
                no_bid = int(no_bid * 100)
            market["_no_bid"] = no_bid

            # Cancel ALL orders on this ticker
            cancel_ok = True
            for order in orders:
                order_id = order.get("order_id", "")
                if not self.dry_run:
                    try:
                        cancel_response = self.client.cancel_order(order_id)
                        cancel_data = cancel_response.get("order", {})
                        fill_count = cancel_data.get("fill_count", order.get("fill_count", 0))
                        self.db.update_order_status(
                            order_id, "cancelled", filled_quantity=fill_count
                        )
                        logger.info(
                            f"[reprice] cancelled {order_id} ({ticker}, {age_hours:.1f}h old, "
                            f"{fill_count} filled)"
                        )
                    except Exception as e:
                        logger.error(f"[reprice] failed to cancel {order_id}: {e}", exc_info=True)
                        cancel_ok = False
                        break
                else:
                    logger.info(
                        f"[reprice] [DRY RUN] would cancel {order_id} ({ticker}, {age_hours:.1f}h old)"
                    )

            if not cancel_ok:
                skipped += len(orders)
                continue

            # Re-place using strategy's calculate_orders (full ladder)
            order_list = strategy.calculate_orders(market)
            if not order_list:
                logger.debug(f"[reprice] {ticker}: calculate_orders returned empty")
                skipped += 1
                continue

            ticker_repriced = 0
            for params in order_list:
                result = placer.place_order_from_params(params, strategy="mentions")

                if result.success:
                    ticker_repriced += 1
                    logger.info(
                        f"[reprice] placed: {ticker} {params.side.upper()} "
                        f"{params.quantity}x @ {params.price_cents}c"
                    )
                    if not result.dry_run:
                        self.db.record_order(
                            client_order_id=result.client_order_id,
                            order_id=result.order_id,
                            strategy="mentions",
                            ticker=ticker,
                            side=params.side,
                            action="reprice",
                            price_cents=params.price_cents,
                            quantity=params.quantity,
                            status="pending",
                            market_snapshot=market,
                        )
                else:
                    logger.warning(
                        f"[reprice] failed to place: {ticker} {params.quantity}x @ {params.price_cents}c "
                        f"error={result.error}"
                    )

            if ticker_repriced > 0:
                repriced += 1
            else:
                skipped += 1

        dry_str = " [DRY]" if self.dry_run else ""
        print(
            f"[{tick_time}] Reprice{dry_str}: "
            f"{repriced} repriced, {skipped} skipped"
        )
        logger.info(
            f"[{tick_time}] Reprice complete: repriced={repriced}, "
            f"skipped={skipped}, dry_run={self.dry_run}"
        )

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    def _get_portfolio_commitment(self) -> Dict[str, int]:
        """Fetch current positions + resting orders from Kalshi API.

        This is the authoritative source of truth for position sizing,
        independent of local DB state. Returns total committed contracts
        per ticker (held positions + unfilled resting orders).
        """
        committed: Dict[str, int] = {}

        # 1. Actual positions (filled contracts we hold)
        cursor = None
        while True:
            try:
                result = self.client.get_positions(
                    limit=100, cursor=cursor, count_filter="position"
                )
            except Exception as e:
                logger.error(f"Error fetching positions: {e}", exc_info=True)
                break

            for pos in result.get("market_positions", []):
                ticker = pos.get("ticker", "")
                count = abs(pos.get("position", 0))
                if ticker and count > 0:
                    committed[ticker] = committed.get(ticker, 0) + count

            cursor = result.get("cursor")
            if not cursor or not result.get("market_positions"):
                break

        # 2. Resting orders (unfilled limit orders on the book)
        cursor = None
        while True:
            try:
                result = self.client.get_orders(
                    status="resting", limit=100, cursor=cursor
                )
            except Exception as e:
                logger.error(f"Error fetching resting orders: {e}", exc_info=True)
                break

            for order in result.get("orders", []):
                ticker = order.get("ticker", "")
                remaining = order.get("remaining_count", 0)
                if ticker and remaining > 0:
                    committed[ticker] = committed.get(ticker, 0) + remaining

            cursor = result.get("cursor")
            if not cursor or not result.get("orders"):
                break

        if committed:
            logger.info(
                f"Portfolio commitment: {len(committed)} tickers, "
                f"{sum(committed.values())} total contracts"
            )

        return committed

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    def _seconds_until_next_tick(self) -> float:
        """Calculate seconds until the next 5-minute wall-clock boundary."""
        now = datetime.now()
        current_minute = now.minute
        current_second = now.second + now.microsecond / 1_000_000

        next_tick_minute = ((current_minute // TICK_INTERVAL) + 1) * TICK_INTERVAL
        minutes_to_wait = next_tick_minute - current_minute
        seconds_to_wait = (minutes_to_wait * 60) - current_second

        return max(0, seconds_to_wait)
