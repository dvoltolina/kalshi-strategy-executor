# tests/test_runner.py
import queue
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

from src.runner import Runner, TICK_INTERVAL, TRADE_MINUTES


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.api_base_url = "https://api.elections.kalshi.com/trade-api/v2"
    config.dry_run = False
    config.grok_api_key = None
    config.database_path = ":memory:"
    config.log_level = "INFO"
    config.max_total_notional_usd = None  # no-cap mode for runner tests
    return config


@pytest.fixture
def mock_client():
    client = MagicMock()
    # Default empty responses for portfolio queries (used by _get_portfolio_commitment)
    client.get_positions.return_value = {"market_positions": []}
    client.get_orders.return_value = {"orders": []}
    return client


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_pending_order_ids.return_value = set()
    db.get_unsettled_orders.return_value = []
    db.get_traded_tickers.return_value = set()
    db.get_recent_order_tickers.return_value = set()
    return db


@pytest.fixture
def runner(mock_config, mock_client, mock_db):
    return Runner(
        config=mock_config,
        client=mock_client,
        db=mock_db,
        dry_run=True,
    )


class TestTickScheduling:
    def test_seconds_until_next_tick(self, runner):
        # Mock datetime.now() to return a specific time
        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 32, 15, 500000)
            secs = runner._seconds_until_next_tick()
            # At 13:32:15.5, next tick is 13:35:00 = 2min 44.5s away
            assert abs(secs - 164.5) < 0.1

    def test_seconds_until_next_tick_on_boundary(self, runner):
        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 30, 0, 0)
            secs = runner._seconds_until_next_tick()
            # Exactly on a boundary, next tick is 5 minutes away
            assert abs(secs - 300.0) < 0.1

    def test_seconds_until_next_tick_just_before(self, runner):
        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 54, 59, 0)
            secs = runner._seconds_until_next_tick()
            # At 13:54:59, next tick is 13:55:00 = 1s away
            assert abs(secs - 1.0) < 0.1


class TestTickExecution:
    def test_trade_tick_runs_all_phases(self, runner):
        """At :30 (trade tick), all four phases run."""
        runner._run_trading = MagicMock()
        runner._run_reprice = MagicMock()
        runner._run_guard = MagicMock()
        runner._run_settlements = MagicMock()
        runner._process_ws_events = MagicMock()

        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 30, 0)
            runner._tick()

        runner._run_trading.assert_called_once()
        runner._run_guard.assert_called_once()
        runner._run_reprice.assert_called_once()
        runner._run_settlements.assert_called_once()

    def test_guard_tick_only_runs_guard(self, runner):
        """At non-trade ticks, only guard runs."""
        runner._run_trading = MagicMock()
        runner._run_reprice = MagicMock()
        runner._run_guard = MagicMock()
        runner._run_settlements = MagicMock()
        runner._process_ws_events = MagicMock()

        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 10, 0)
            runner._tick()

        runner._run_trading.assert_not_called()
        runner._run_reprice.assert_not_called()
        runner._run_guard.assert_called_once()
        runner._run_settlements.assert_not_called()

    def test_00_runs_reprice_and_trade(self, runner):
        """At :00, reprice + trade + settle all run."""
        runner._run_trading = MagicMock()
        runner._run_reprice = MagicMock()
        runner._run_guard = MagicMock()
        runner._run_settlements = MagicMock()
        runner._process_ws_events = MagicMock()

        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 0, 0)
            runner._tick()

        runner._run_trading.assert_called_once()
        runner._run_reprice.assert_called_once()
        runner._run_settlements.assert_called_once()

    def test_trade_tick_order_is_guard_reprice_trade_settle(self, runner):
        """Verify execution order at :30: guard -> reprice -> trade -> settle."""
        call_order = []
        runner._run_reprice = MagicMock(side_effect=lambda t: call_order.append("reprice"))
        runner._run_trading = MagicMock(side_effect=lambda t: call_order.append("trade"))
        runner._run_guard = MagicMock(side_effect=lambda t: call_order.append("guard"))
        runner._run_settlements = MagicMock(side_effect=lambda t: call_order.append("settle"))
        runner._process_ws_events = MagicMock()

        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 30, 0)
            runner._tick()

        assert call_order == ["guard", "reprice", "trade", "settle"]


class TestProcessWsEvents:
    def test_fully_executed_order_updates_db(self, runner, mock_client, mock_db):
        runner._fill_queue.put(("kalshi-1", {"order_id": "kalshi-1", "count": 50}))

        mock_client.get_order.return_value = {
            "order": {"status": "executed", "fill_count": 50, "initial_count": 50}
        }

        runner._process_ws_events()

        mock_db.update_order_status.assert_called_once_with(
            "kalshi-1", "filled", filled_quantity=50
        )

    def test_partial_fill_not_updated(self, runner, mock_client, mock_db):
        """Partial fills (resting) don't change DB status."""
        runner._fill_queue.put(("kalshi-1", {"order_id": "kalshi-1", "count": 5}))

        mock_client.get_order.return_value = {
            "order": {"status": "resting", "fill_count": 5, "initial_count": 50}
        }

        runner._process_ws_events()
        mock_db.update_order_status.assert_not_called()

    def test_api_error_handled(self, runner, mock_client, mock_db):
        runner._fill_queue.put(("kalshi-1", {}))
        mock_client.get_order.side_effect = Exception("API down")

        # Should not raise
        runner._process_ws_events()
        mock_db.update_order_status.assert_not_called()

    def test_reconnect_triggers_reconciliation(self, runner, mock_client, mock_db):
        """WS reconnect flag triggers mini-reconciliation on next tick."""
        mock_reconciler = MagicMock()
        runner._reconciler = mock_reconciler
        runner._needs_reconciliation.set()

        runner._process_ws_events()

        mock_reconciler.reconcile_orders.assert_called_once()
        mock_reconciler.reconcile_settlements.assert_called_once()
        assert not runner._needs_reconciliation.is_set()

    def test_no_reconciliation_without_flag(self, runner):
        mock_reconciler = MagicMock()
        runner._reconciler = mock_reconciler
        # _needs_reconciliation starts cleared (not set)

        runner._process_ws_events()

        mock_reconciler.reconcile_orders.assert_not_called()

    def test_settlement_events_trigger_check(self, runner):
        """WS settlement events trigger an immediate settlement check."""
        runner._settlement_queue.put(("MKT-A", "yes"))
        runner._settlement_queue.put(("MKT-B", "no"))

        runner._run_settlements = MagicMock()

        runner._process_ws_events()

        assert runner._settlement_queue.empty()
        runner._run_settlements.assert_called_once()


class TestWsCallbacks:
    def test_fill_queued(self, runner):
        runner._on_ws_fill("ord-1", {"count": 5})
        assert not runner._fill_queue.empty()
        order_id, data = runner._fill_queue.get_nowait()
        assert order_id == "ord-1"

    def test_settlement_queued(self, runner):
        runner._on_ws_settlement("MKT-A", "yes")
        assert not runner._settlement_queue.empty()
        ticker, result = runner._settlement_queue.get_nowait()
        assert ticker == "MKT-A"
        assert result == "yes"


class TestWsReconnectCallback:
    def test_reconnect_sets_flag(self, runner):
        runner._on_ws_reconnect()
        assert runner._needs_reconciliation.is_set()


class TestWebSocketUrlDerivation:
    def test_derives_ws_url(self, runner, mock_config):
        """The WS URL is correctly derived from the REST URL."""
        mock_config.api_base_url = "https://api.elections.kalshi.com/trade-api/v2"

        with patch("src.ws_client.KalshiWebSocket") as MockWS:
            runner._start_websocket()
            if MockWS.called:
                _, kwargs = MockWS.call_args
                assert kwargs["base_ws_url"] == "wss://api.elections.kalshi.com/trade-api/ws/v2"


class TestStop:
    def test_stop_closes_db_and_ws(self, runner):
        runner._ws = MagicMock()
        runner.stop()

        runner._ws.stop.assert_called_once()
        runner.db.close.assert_called_once()
        assert runner._running is False


class TestGuardInit:
    def test_guard_disabled_without_grok_key(self, runner, mock_config):
        mock_config.grok_api_key = None
        runner._init_guard()
        assert runner._guard is None

    @patch("src.runner.discover_guarded_strategies", return_value=["mentions"])
    @patch("src.order_guard.OrderGuard")
    def test_guard_initialized_with_key(self, MockGuard, mock_discover, runner, mock_config):
        mock_config.grok_api_key = "test-key"  # pragma: allowlist secret
        runner._init_guard()
        MockGuard.assert_called_once()
        # Verify strategies param was passed
        call_kwargs = MockGuard.call_args[1]
        assert call_kwargs["strategies"] == ["mentions"]
        assert runner._guard is not None

    @patch("src.runner.discover_guarded_strategies", return_value=[])
    def test_guard_disabled_no_guarded_strategies(self, mock_discover, runner, mock_config):
        mock_config.grok_api_key = "test-key"  # pragma: allowlist secret
        runner._init_guard()
        assert runner._guard is None


class TestWarmGuardCache:
    def test_warm_guard_cache_with_positions(self, runner, mock_client):
        """Positions found: warm_cache() called with tickers."""
        mock_guard = MagicMock()
        mock_guard.warm_cache.return_value = {
            "tickers": 2, "events_found": 1,
            "events_cached": 0, "events_queried": 1, "errors": 0,
        }
        runner._guard = mock_guard

        mock_client.get_positions.return_value = {
            "market_positions": [
                {"ticker": "MKT-A", "position": 10},
                {"ticker": "MKT-B", "position": 5},
            ],
            "cursor": "",
        }

        runner._warm_guard_cache()

        mock_guard.warm_cache.assert_called_once_with(["MKT-A", "MKT-B"])

    def test_warm_guard_cache_no_guard(self, runner):
        """Guard is None: early return, no API calls."""
        runner._guard = None
        runner._warm_guard_cache()
        runner.client.get_positions.assert_not_called()

    def test_warm_guard_cache_no_positions(self, runner, mock_client):
        """Empty portfolio: warm_cache() not called."""
        mock_guard = MagicMock()
        runner._guard = mock_guard

        mock_client.get_positions.return_value = {
            "market_positions": [],
            "cursor": "",
        }

        runner._warm_guard_cache()

        mock_guard.warm_cache.assert_not_called()


class TestMultiStrategyTrading:
    @patch("src.runner.discover_strategies", return_value=["mentions", "longshot"])
    def test_run_trading_calls_all_strategies(self, mock_discover, runner):
        """Trading loop runs all discovered strategies."""
        runner._run_single_strategy = MagicMock(return_value=(1, 0))

        runner._run_trading("13:25:00")

        assert runner._run_single_strategy.call_count == 2
        strategy_names = [
            call[0][0] for call in runner._run_single_strategy.call_args_list
        ]
        assert "mentions" in strategy_names
        assert "longshot" in strategy_names

    @patch("src.runner.discover_strategies", return_value=[])
    def test_run_trading_no_strategies(self, mock_discover, runner):
        """Trading handles zero discovered strategies gracefully."""
        runner._run_single_strategy = MagicMock()

        runner._run_trading("13:25:00")

        runner._run_single_strategy.assert_not_called()


class TestExplicitStrategyList:
    def test_runner_uses_explicit_strategies(self, mock_config, mock_client, mock_db):
        """When strategies are passed explicitly, only those are traded."""
        r = Runner(
            config=mock_config, client=mock_client, db=mock_db,
            dry_run=True, strategies=["mentions"],
        )
        r._run_single_strategy = MagicMock(return_value=(1, 0))

        r._run_trading("13:25:00")

        assert r._run_single_strategy.call_count == 1
        assert r._run_single_strategy.call_args_list[0][0][0] == "mentions"

    @patch("src.runner.discover_strategies", return_value=["mentions", "longshot"])
    def test_runner_falls_back_to_discover(self, mock_discover, mock_config, mock_client, mock_db):
        """When no explicit strategies, falls back to discover_strategies()."""
        r = Runner(
            config=mock_config, client=mock_client, db=mock_db,
            dry_run=True, strategies=None,
        )
        r._run_single_strategy = MagicMock(return_value=(1, 0))

        r._run_trading("13:25:00")

        mock_discover.assert_called_once()
        assert r._run_single_strategy.call_count == 2


class TestTradingEventGuard:
    """Trading path should skip markets whose events have started."""

    @patch("src.strategies.load_strategy_config")
    @patch("src.strategies.get_strategy_class")
    def test_trading_skips_started_event(
        self, mock_get_class, mock_load, runner, mock_client
    ):
        """_run_single_strategy skips markets whose event has started."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)

        mock_load.return_value = {
            "name": "mentions",
            "filters": {},
            "order": {"side": "no", "contracts_per_market": 500},
        }

        mock_strategy = MagicMock()
        mock_strategy.find_markets.return_value = [
            {"ticker": "MKT-A", "event_ticker": "EVT-STARTED"},
        ]
        mock_get_class.return_value = lambda client, config: mock_strategy

        # Set up guard with event cache showing event already started
        guard = MagicMock()
        guard.cancel_buffer_minutes = 15
        mock_cache = MagicMock()
        mock_cache.get.return_value = {
            "estimated_start_utc": now - timedelta(minutes=30),
            "confidence": "high",
        }
        guard.event_cache = mock_cache
        runner._guard = guard

        mock_client.get_positions.return_value = {"market_positions": [], "cursor": ""}
        mock_client.get_orders.return_value = {"orders": [], "cursor": ""}

        from src.order_placer import OrderPlacer

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            MockPlacer.return_value = mock_placer

            successes, failures = runner._run_single_strategy(
                "mentions", mock_placer, "13:00:00", {}
            )

            # calculate_orders should never be called — market skipped
            mock_strategy.calculate_orders.assert_not_called()
            assert successes == 0

    @patch("src.strategies.load_strategy_config")
    @patch("src.strategies.get_strategy_class")
    def test_trading_allows_future_event(
        self, mock_get_class, mock_load, runner, mock_client
    ):
        """_run_single_strategy allows markets whose event is far in the future."""
        from datetime import timezone, timedelta
        from src.strategies.base import OrderParams

        now = datetime.now(timezone.utc)

        mock_load.return_value = {
            "name": "mentions",
            "filters": {},
            "order": {"side": "no", "contracts_per_market": 500},
        }

        mock_strategy = MagicMock()
        mock_strategy.find_markets.return_value = [
            {"ticker": "MKT-A", "event_ticker": "EVT-FUTURE"},
        ]
        mock_strategy.calculate_orders.return_value = [OrderParams(
            ticker="MKT-A", side="no", action="buy",
            price_cents=70, quantity=500, post_only=True,
            market_snapshot={},
        )]
        mock_get_class.return_value = lambda client, config: mock_strategy

        # Event is 2 hours away — should proceed
        guard = MagicMock()
        guard.cancel_buffer_minutes = 15
        mock_cache = MagicMock()
        mock_cache.get.return_value = {
            "estimated_start_utc": now + timedelta(hours=2),
            "confidence": "high",
        }
        guard.event_cache = mock_cache
        runner._guard = guard

        mock_client.get_positions.return_value = {"market_positions": [], "cursor": ""}
        mock_client.get_orders.return_value = {"orders": [], "cursor": ""}

        mock_placer = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.dry_run = True
        mock_placer.place_order_from_params.return_value = mock_result

        successes, failures = runner._run_single_strategy(
            "mentions", mock_placer, "13:00:00", {}
        )

        # calculate_orders SHOULD be called — event is in the future
        mock_strategy.calculate_orders.assert_called_once()
        assert successes == 1


class TestGuardCheckEvent:
    """Tests for _guard_check_event — milestones query for uncached events."""

    def test_cached_event_safe_allows_order(self, runner):
        """Cached event far in the future → allow, no milestones call."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        guard.event_cache.get.return_value = {
            "estimated_start_utc": now + timedelta(hours=5),
            "confidence": "high",
        }
        runner._guard = guard

        assert runner._guard_check_event("EVT-A", "MKT-A", "mentions") is True
        guard._query_milestones.assert_not_called()

    def test_cached_event_imminent_blocks_order(self, runner):
        """Cached event starting in 5 minutes → block, no milestones call."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        guard.event_cache.get.return_value = {
            "estimated_start_utc": now + timedelta(minutes=5),
            "confidence": "high",
        }
        runner._guard = guard

        assert runner._guard_check_event("EVT-A", "MKT-A", "mentions") is False
        guard._query_milestones.assert_not_called()

    def test_uncached_event_milestones_resolves_safe(self, runner):
        """Uncached event → milestones finds data → event safe → allow."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        # First call: not cached. Second call (after milestones): cached.
        guard.event_cache.get.side_effect = [
            None,
            {"estimated_start_utc": now + timedelta(hours=5), "confidence": "high"},
        ]
        guard._query_milestones.return_value = []  # No fallback — milestones succeeded
        runner._guard = guard

        assert runner._guard_check_event("EVT-NEW", "MKT-A", "mentions") is True
        guard._query_milestones.assert_called_once()

    def test_uncached_event_milestones_resolves_imminent(self, runner):
        """Uncached event → milestones finds data → event imminent → block."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        guard.event_cache.get.side_effect = [
            None,
            {"estimated_start_utc": now + timedelta(minutes=3), "confidence": "high"},
        ]
        guard._query_milestones.return_value = []  # Milestones succeeded
        runner._guard = guard

        assert runner._guard_check_event("EVT-NEW", "MKT-A", "mentions") is False
        guard._query_milestones.assert_called_once()

    def test_uncached_event_no_milestones_tries_grok_then_blocks(self, runner):
        """Uncached event → milestones has NO data → Grok called → still no data → block."""
        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        # First get: not cached. After milestones fallback + Grok: still not cached.
        guard.event_cache.get.side_effect = [None, None]
        guard._query_milestones.return_value = ["EVT-UNKNOWN"]  # Needs Grok fallback
        runner._guard = guard

        assert runner._guard_check_event("EVT-UNKNOWN", "MKT-A", "mentions") is False
        guard._query_milestones.assert_called_once()
        guard._query_start_times.assert_called_once()

    def test_uncached_event_no_milestones_grok_resolves_safe(self, runner):
        """Uncached event → milestones has NO data → Grok resolves → event safe → allow."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        # First get: not cached. After milestones: fallback. After Grok: cached safe.
        guard.event_cache.get.side_effect = [
            None,
            {"estimated_start_utc": now + timedelta(hours=5), "confidence": "high"},
        ]
        guard._query_milestones.return_value = ["EVT-GROK"]  # Needs Grok fallback
        runner._guard = guard

        assert runner._guard_check_event("EVT-GROK", "MKT-A", "mentions") is True
        guard._query_milestones.assert_called_once()
        guard._query_start_times.assert_called_once()

    def test_uncached_event_no_milestones_grok_resolves_imminent(self, runner):
        """Uncached event → milestones has NO data → Grok resolves → event imminent → block."""
        from datetime import timezone, timedelta

        now = datetime.now(timezone.utc)
        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        # First get: not cached. After Grok: cached but imminent.
        guard.event_cache.get.side_effect = [
            None,
            {"estimated_start_utc": now + timedelta(minutes=3), "confidence": "high"},
        ]
        guard._query_milestones.return_value = ["EVT-SOON"]  # Needs Grok fallback
        runner._guard = guard

        assert runner._guard_check_event("EVT-SOON", "MKT-A", "mentions") is False
        guard._query_start_times.assert_called_once()

    @patch("src.strategies.load_strategy_config")
    @patch("src.strategies.get_strategy_class")
    def test_trading_blocks_uncached_event_without_milestones_or_grok(
        self, mock_get_class, mock_load, runner, mock_client
    ):
        """Integration: trading path blocks orders when neither milestones nor Grok has data."""
        from src.strategies.base import OrderParams

        mock_load.return_value = {
            "name": "mentions",
            "filters": {},
            "order": {"side": "no", "contracts_per_market": 500},
        }

        mock_strategy = MagicMock()
        mock_strategy.find_markets.return_value = [
            {"ticker": "MKT-NEW", "event_ticker": "EVT-UNKNOWN"},
        ]
        mock_get_class.return_value = lambda client, config: mock_strategy

        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        # Never gets cached — milestones returns fallback, Grok also fails
        guard.event_cache.get.return_value = None
        guard._query_milestones.return_value = ["EVT-UNKNOWN"]
        runner._guard = guard

        mock_client.get_positions.return_value = {"market_positions": [], "cursor": ""}
        mock_client.get_orders.return_value = {"orders": [], "cursor": ""}

        mock_placer = MagicMock()
        successes, failures = runner._run_single_strategy(
            "mentions", mock_placer, "13:00:00", {}
        )

        # Order should be blocked — calculate_orders never called
        mock_strategy.calculate_orders.assert_not_called()
        mock_placer.place_order_from_params.assert_not_called()
        # Grok was attempted as fallback
        guard._query_start_times.assert_called_once()
        assert successes == 0


# Shared mentions config fixture for reprice tests
MENTIONS_CONFIG = {
    "name": "mentions",
    "enabled": True,
    "filters": {"categories": ["Mentions"], "min_no_bid_price": 0.10, "max_no_bid_price": 0.85, "min_volume": 0, "min_close_hours": 0, "max_close_hours": 30},
    "order": {
        "side": "no",
        "contracts_per_market": 500,
        "pricing": {"mode": "ladder", "levels": 7, "start_offset": 0, "step": -1},
        "post_only": True,
    },
    "reprice": {"enabled": True, "after_hours": 6},
}


def _make_resting_order(order_id, ticker, hours_old=7, fill_count=0, event_ticker=None):
    """Helper to create a resting order dict with a created_time N hours ago."""
    from datetime import timezone, timedelta
    created = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    order = {
        "order_id": order_id,
        "ticker": ticker,
        "created_time": created.isoformat(),
        "fill_count": fill_count,
        "remaining_count": 500,
    }
    if event_ticker:
        order["event_ticker"] = event_ticker
    return order


class TestRepriceTickDispatch:
    """Reprice runs at every trade tick (:00 and :30)."""

    def test_reprice_called_at_00(self, runner):
        """At :00, reprice runs."""
        runner._run_reprice = MagicMock()
        runner._run_guard = MagicMock()
        runner._run_trading = MagicMock()
        runner._run_settlements = MagicMock()
        runner._process_ws_events = MagicMock()

        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 0, 0)
            runner._tick()

        runner._run_reprice.assert_called_once()
        runner._run_trading.assert_called_once()

    def test_reprice_called_at_30(self, runner):
        """At :30, reprice runs."""
        runner._run_reprice = MagicMock()
        runner._run_guard = MagicMock()
        runner._run_trading = MagicMock()
        runner._run_settlements = MagicMock()
        runner._process_ws_events = MagicMock()

        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 14, 30, 0)
            runner._tick()

        runner._run_reprice.assert_called_once()
        runner._run_trading.assert_called_once()

    def test_reprice_not_called_at_guard_only_tick(self, runner):
        """At :10 (guard-only tick), reprice should NOT run."""
        runner._run_reprice = MagicMock()
        runner._run_guard = MagicMock()
        runner._run_trading = MagicMock()
        runner._run_settlements = MagicMock()
        runner._process_ws_events = MagicMock()

        with patch("src.runner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 10, 13, 10, 0)
            runner._tick()

        runner._run_reprice.assert_not_called()
        runner._run_guard.assert_called_once()


class TestRepriceLogic:
    """Tests for _run_reprice() business logic."""

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_stale_order_repriced(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """Stale order (7h old) -> cancel + re-place ladder at new prices."""
        mock_load.return_value = MENTIONS_CONFIG

        from src.strategies.base import OrderParams
        mock_strategy = MagicMock()
        mock_strategy.calculate_orders.return_value = [
            OrderParams(ticker="MKT-A", side="no", action="buy", price_cents=71, quantity=99, post_only=True, market_snapshot={}),
            OrderParams(ticker="MKT-A", side="no", action="buy", price_cents=72, quantity=88, post_only=True, market_snapshot={}),
        ]
        mock_get_class.return_value = lambda client, config: mock_strategy

        # DB returns this order as a mentions pending order
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {"status": "open", "no_bid": 70, "no_ask": 80, "ticker": "MKT-A"},
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.dry_run = True
            mock_placer.place_order_from_params.return_value = mock_result
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            # 2 ladder orders placed
            assert mock_placer.place_order_from_params.call_count == 2
            first_params = mock_placer.place_order_from_params.call_args_list[0][0][0]
            assert first_params.ticker == "MKT-A"
            assert first_params.price_cents == 71

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_young_order_skipped(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """Young order (3h old) -> skip, no cancel."""
        mock_load.return_value = MENTIONS_CONFIG
        mock_get_class.return_value = MagicMock()
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        young_order = _make_resting_order("order-1", "MKT-A", hours_old=3)
        mock_client.get_orders.return_value = {
            "orders": [young_order], "cursor": "",
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            mock_client.cancel_order.assert_not_called()
            mock_placer.place_order_from_params.assert_not_called()

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_partial_fill_repriced_with_accurate_fill_count(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """Partial fill -> cancel uses fill_count from cancel response."""
        mock_load.return_value = MENTIONS_CONFIG
        runner.dry_run = False
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        from src.strategies.base import OrderParams
        mock_strategy = MagicMock()
        mock_strategy.calculate_orders.return_value = [
            OrderParams(ticker="MKT-A", side="no", action="buy", price_cents=51, quantity=500, post_only=True, market_snapshot={}),
        ]
        mock_get_class.return_value = lambda client, config: mock_strategy

        # Initial fetch shows 200 filled, but by cancel time it's 250
        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7, fill_count=200)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {"status": "open", "no_bid": 50, "no_ask": 60, "ticker": "MKT-A"},
        }
        # Cancel response has the accurate fill_count (250, not stale 200)
        mock_client.cancel_order.return_value = {
            "order": {"fill_count": 250, "status": "canceled"},
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.dry_run = False
            mock_result.client_order_id = "new-uuid"
            mock_result.order_id = "new-order-1"
            mock_placer.place_order_from_params.return_value = mock_result
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            mock_client.cancel_order.assert_called_once_with("order-1")
            # Uses 250 from cancel response, not stale 200
            mock_db.update_order_status.assert_called_once_with(
                "order-1", "cancelled", filled_quantity=250
            )

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_closed_market_skipped(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """Market closed -> skip (no cancel)."""
        mock_load.return_value = MENTIONS_CONFIG
        mock_get_class.return_value = MagicMock()
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {"status": "closed", "no_bid": 70, "no_ask": 75, "ticker": "MKT-A"},
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            mock_client.cancel_order.assert_not_called()
            mock_placer.place_order_from_params.assert_not_called()

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_no_liquidity_skipped(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """No liquidity (no_bid=0) -> skip (no cancel)."""
        mock_load.return_value = MENTIONS_CONFIG
        mock_get_class.return_value = MagicMock()
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {"status": "open", "no_bid": 0, "no_ask": 97, "ticker": "MKT-A"},
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            mock_client.cancel_order.assert_not_called()
            mock_placer.place_order_from_params.assert_not_called()

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_ladder_repricing(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """Reprice uses strategy.calculate_orders() to place ladder."""
        mock_load.return_value = MENTIONS_CONFIG
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        from src.strategies.base import OrderParams
        mock_strategy = MagicMock()
        mock_strategy.calculate_orders.return_value = [
            OrderParams(ticker="MKT-A", side="no", action="buy", price_cents=71, quantity=99, post_only=True, market_snapshot={}),
            OrderParams(ticker="MKT-A", side="no", action="buy", price_cents=72, quantity=88, post_only=True, market_snapshot={}),
            OrderParams(ticker="MKT-A", side="no", action="buy", price_cents=73, quantity=78, post_only=True, market_snapshot={}),
        ]
        mock_get_class.return_value = lambda client, config: mock_strategy

        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {"status": "open", "no_bid": 70, "no_ask": 80, "ticker": "MKT-A"},
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.dry_run = True
            mock_placer.place_order_from_params.return_value = mock_result
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            # Strategy's calculate_orders was called with market data (including _no_bid)
            mock_strategy.calculate_orders.assert_called_once()
            call_market = mock_strategy.calculate_orders.call_args[0][0]
            assert call_market["_no_bid"] == 70

            # All 3 ladder levels placed
            assert mock_placer.place_order_from_params.call_count == 3

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_guard_event_imminent_skipped(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """Reprice skips orders whose event is imminent per guard cache."""
        mock_load.return_value = MENTIONS_CONFIG
        mock_get_class.return_value = MagicMock()
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {
                "status": "active", "no_bid": 70, "no_ask": 75,
                "ticker": "MKT-A", "event_ticker": "EVT-STARTED",
            },
        }

        from datetime import timezone, timedelta

        guard = MagicMock()
        guard.cancel_buffer_minutes = 10
        mock_cache = MagicMock()
        now = datetime.now(timezone.utc)
        mock_cache.get.return_value = {
            "estimated_start_utc": now - timedelta(minutes=30),
            "confidence": "high",
        }
        guard.event_cache = mock_cache
        runner._guard = guard

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            mock_client.cancel_order.assert_not_called()
            mock_placer.place_order_from_params.assert_not_called()

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_disabled_config_returns_early(self, mock_load, mock_get_class, runner):
        """When reprice.enabled is false, no API calls are made."""
        config = {**MENTIONS_CONFIG, "reprice": {"enabled": False}}
        mock_load.return_value = config

        runner._run_reprice("13:00:00")

        runner.client.get_orders.assert_not_called()
        runner.client.get_market.assert_not_called()

    @patch("src.strategies.load_strategy_config")
    def test_missing_config_skips_gracefully(self, mock_load, runner):
        """FileNotFoundError from config loading -> early return."""
        mock_load.side_effect = FileNotFoundError("not found")

        runner._run_reprice("13:00:00")

        runner.client.get_orders.assert_not_called()

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_live_reprice_records_in_db(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """Live reprice: cancel + new orders recorded with action='reprice'."""
        mock_load.return_value = MENTIONS_CONFIG
        runner.dry_run = False
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        from src.strategies.base import OrderParams
        mock_strategy = MagicMock()
        mock_strategy.calculate_orders.return_value = [
            OrderParams(ticker="MKT-A", side="no", action="buy", price_cents=71, quantity=99, post_only=True, market_snapshot={}),
        ]
        mock_get_class.return_value = lambda client, config: mock_strategy

        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7, fill_count=100)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {"status": "open", "no_bid": 70, "no_ask": 80, "ticker": "MKT-A"},
        }
        # Cancel response returns accurate fill_count
        mock_client.cancel_order.return_value = {
            "order": {"fill_count": 100, "status": "canceled"},
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.dry_run = False
            mock_result.client_order_id = "new-uuid"
            mock_result.order_id = "new-order-1"
            mock_placer.place_order_from_params.return_value = mock_result
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            # Old order cancelled — fill count from cancel response
            mock_client.cancel_order.assert_called_once_with("order-1")
            mock_db.update_order_status.assert_called_once_with(
                "order-1", "cancelled", filled_quantity=100
            )
            # New order recorded with action="reprice"
            mock_db.record_order.assert_called_once()
            call_kwargs = mock_db.record_order.call_args[1]
            assert call_kwargs["strategy"] == "mentions"
            assert call_kwargs["ticker"] == "MKT-A"
            assert call_kwargs["action"] == "reprice"
            assert call_kwargs["status"] == "pending"

            # Verify DB was queried with exclude_action="reprice"
            mock_db.get_pending_order_ids.assert_called_once_with(
                strategy="mentions", exclude_action="reprice"
            )

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_cancel_failure_skips_replacement(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """If cancel fails, skip the replacement order."""
        mock_load.return_value = MENTIONS_CONFIG
        mock_get_class.return_value = MagicMock()
        runner.dry_run = False
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        stale_order = _make_resting_order("order-1", "MKT-A", hours_old=7)
        mock_client.get_orders.return_value = {
            "orders": [stale_order], "cursor": "",
        }
        mock_client.get_market.return_value = {
            "market": {"status": "open", "no_bid": 70, "no_ask": 80, "ticker": "MKT-A"},
        }
        mock_client.cancel_order.side_effect = Exception("Cancel failed")

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            mock_placer.place_order_from_params.assert_not_called()

    @patch("src.strategies.get_strategy_class")
    @patch("src.strategies.load_strategy_config")
    def test_no_matching_resting_orders(self, mock_load, mock_get_class, runner, mock_client, mock_db):
        """No resting orders match mentions -> early return."""
        mock_load.return_value = MENTIONS_CONFIG
        mock_get_class.return_value = MagicMock()
        mock_db.get_pending_order_ids.return_value = {"order-1"}

        # Resting orders exist but none match mentions order IDs
        mock_client.get_orders.return_value = {
            "orders": [{"order_id": "other-order", "ticker": "MKT-B"}],
            "cursor": "",
        }

        with patch("src.order_placer.OrderPlacer") as MockPlacer:
            mock_placer = MagicMock()
            MockPlacer.return_value = mock_placer

            runner._run_reprice("13:00:00")

            mock_client.cancel_order.assert_not_called()
            mock_placer.place_order_from_params.assert_not_called()
