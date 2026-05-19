# tests/test_strategies/test_mentions.py
import time
import pytest
from unittest.mock import MagicMock, patch

from src.strategies.mentions import MentionsStrategy
from src.strategies.base import OrderParams


class TestMentionsStrategy:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def config(self):
        return {
            "name": "mentions",
            "filters": {
                "categories": ["Mentions"],
                "min_no_bid_price": 0.20,
                "max_no_bid_price": 0.70,
                "min_volume": 0,
                "min_close_hours": 0,
                "max_close_hours": 30,
            },
            "order": {
                "side": "no",
                "contracts_per_market": 15,
                "pricing": {
                    "mode": "offset",
                    "offset_cents": -2,
                },
                "post_only": True,
            },
        }

    @pytest.fixture
    def ladder_config(self):
        return {
            "name": "mentions",
            "filters": {
                "categories": ["Mentions"],
                "min_no_bid_price": 0.20,
                "max_no_bid_price": 0.85,
                "min_volume": 0,
                "min_close_hours": 0,
                "max_close_hours": 30,
            },
            "order": {
                "side": "no",
                "contracts_per_market": 500,
                "pricing": {
                    "mode": "ladder",
                    "levels": 7,
                    "start_offset": 0,
                    "step": -1,
                },
                "post_only": True,
            },
        }

    def test_find_markets_uses_close_time_filter(self, mock_client, config):
        mock_client.get_markets.return_value = {
            "markets": [
                {"ticker": "MENTION-A", "event_ticker": "EVT-A", "yes_bid": 50, "no_bid": 50},
            ],
            "cursor": None,
        }
        mock_client.get_event.return_value = {"event": {"category": "Mentions"}}

        strategy = MentionsStrategy(mock_client, config)

        with patch("src.strategies.mentions.time.time", return_value=1000000):
            strategy.find_markets()

        call_kwargs = mock_client.get_markets.call_args[1]
        assert call_kwargs["min_close_ts"] == 1000000 + (0 * 3600)
        assert call_kwargs["max_close_ts"] == 1000000 + (30 * 3600)

    def test_calculate_order_offset(self, mock_client, config):
        """Order price is best bid minus 2c."""
        strategy = MentionsStrategy(mock_client, config)

        market = {
            "ticker": "MENTION-TEST",
            "event_ticker": "EVT-TEST",
            "_no_bid": 50,
        }

        order = strategy.calculate_order(market)

        assert order is not None
        assert order.price_cents == 48
        assert order.quantity == 15
        assert order.post_only is True

    def test_calculate_order_skips_if_below_min(self, mock_client, config):
        """Offset that drops below min_no_bid_price is skipped."""
        strategy = MentionsStrategy(mock_client, config)

        market = {
            "ticker": "MENTION-TEST",
            "_no_bid": 21,
        }

        order = strategy.calculate_order(market)

        assert order is None

    # ------------------------------------------------------------------
    # Ladder tests
    # ------------------------------------------------------------------

    def test_ladder_quantities(self, mock_client, ladder_config):
        """Geometric allocation sums to total, first ~2x last."""
        quantities = MentionsStrategy._ladder_quantities(7, 500)
        assert sum(quantities) == 500
        assert len(quantities) == 7
        # First level should be roughly 2x the last level
        assert 1.5 <= quantities[0] / quantities[-1] <= 2.5
        # Should be monotonically decreasing
        for i in range(len(quantities) - 1):
            assert quantities[i] >= quantities[i + 1]

    def test_ladder_quantities_single_level(self, mock_client, ladder_config):
        """Single level gets all contracts."""
        quantities = MentionsStrategy._ladder_quantities(1, 500)
        assert quantities == [500]

    def test_calculate_orders_ladder(self, mock_client, ladder_config):
        """Ladder mode produces 7 orders going DOWN from best bid."""
        strategy = MentionsStrategy(mock_client, ladder_config)

        market = {
            "ticker": "MENTION-TEST",
            "event_ticker": "EVT-TEST",
            "_no_bid": 50,
            "no_ask": 60,
        }

        orders = strategy.calculate_orders(market)

        assert len(orders) == 7
        # Prices: 50, 49, 48, 47, 46, 45, 44
        for i, order in enumerate(orders):
            assert order.price_cents == 50 - i
            assert order.side == "no"
            assert order.post_only is True
            assert order.ticker == "MENTION-TEST"

        # Total contracts sum to 500
        total = sum(o.quantity for o in orders)
        assert total == 500

        # First level (at bid) gets most contracts
        assert orders[0].quantity > orders[-1].quantity

    def test_ladder_respects_min_price(self, mock_client, ladder_config):
        """Levels below min_no_bid_price are dropped."""
        strategy = MentionsStrategy(mock_client, ladder_config)

        # min_no_bid_price = 0.20 = 20c. _no_bid = 23, step=-1
        # Levels: 23, 22, 21, 20 — levels 19 and below are dropped
        market = {
            "ticker": "MENTION-TEST",
            "_no_bid": 23,
            "no_ask": 90,
        }

        orders = strategy.calculate_orders(market)

        assert len(orders) == 4
        assert orders[0].price_cents == 23
        assert orders[-1].price_cents == 20

    def test_calculate_order_delegates_to_ladder(self, mock_client, ladder_config):
        """Single-order stub returns first ladder order."""
        strategy = MentionsStrategy(mock_client, ladder_config)

        market = {
            "ticker": "MENTION-TEST",
            "_no_bid": 50,
            "no_ask": 60,
        }

        order = strategy.calculate_order(market)

        assert order is not None
        assert order.price_cents == 50  # First ladder level (matches best bid)

    def test_ladder_empty_when_no_bid_missing(self, mock_client, ladder_config):
        """Returns [] when _no_bid is None."""
        strategy = MentionsStrategy(mock_client, ladder_config)

        market = {
            "ticker": "MENTION-TEST",
        }

        orders = strategy.calculate_orders(market)
        assert orders == []

    def test_ladder_stops_at_price_floor(self, mock_client, ladder_config):
        """Ladder stops when price would go below 1c."""
        strategy = MentionsStrategy(mock_client, ladder_config)

        # _no_bid = 3, step=-1: prices would be 3, 2, 1, 0(invalid)
        # min_no_bid_price = 20c, so all levels below 20c are dropped
        # Actually 3c < 20c min, so NO levels survive
        market = {
            "ticker": "MENTION-TEST",
            "_no_bid": 3,
            "no_ask": 90,
        }

        orders = strategy.calculate_orders(market)
        assert orders == []

    def test_ladder_below_1c_breaks(self, mock_client):
        """Ladder breaks at price < 1 even without min_price filter."""
        config = {
            "name": "mentions",
            "filters": {
                "categories": ["Mentions"],
                "min_no_bid_price": 0.01,  # 1c min — effectively no filter
                "max_no_bid_price": 0.85,
            },
            "order": {
                "side": "no",
                "contracts_per_market": 500,
                "pricing": {"mode": "ladder", "levels": 7, "start_offset": 0, "step": -1},
                "post_only": True,
            },
        }
        strategy = MentionsStrategy(MagicMock(), config)

        market = {"ticker": "MENTION-TEST", "_no_bid": 4, "no_ask": 90}
        orders = strategy.calculate_orders(market)

        # Prices: 4, 3, 2, 1 — stops before 0
        assert len(orders) == 4
        assert orders[-1].price_cents == 1

    def test_find_markets_filters_by_volume(self, mock_client, config):
        """Markets below min_volume are excluded."""
        config["filters"]["min_volume"] = 500
        mock_client.get_markets.return_value = {
            "markets": [
                {"ticker": "MENTION-A", "event_ticker": "EVT-A", "yes_bid": 50, "no_bid": 50, "volume": 1000},
                {"ticker": "MENTION-B", "event_ticker": "EVT-B", "yes_bid": 50, "no_bid": 50, "volume": 100},
                {"ticker": "MENTION-C", "event_ticker": "EVT-C", "yes_bid": 50, "no_bid": 50, "volume": 500},
            ],
            "cursor": None,
        }
        mock_client.get_series.return_value = {"series": [{"ticker": "EVT"}]}

        strategy = MentionsStrategy(mock_client, config)

        with patch("src.strategies.mentions.time.time", return_value=1000000):
            markets = strategy.find_markets()

        # MENTION-B (volume=100) should be excluded; A (1000) and C (500) kept
        tickers = [m["ticker"] for m in markets]
        assert "MENTION-A" in tickers
        assert "MENTION-C" in tickers
        assert "MENTION-B" not in tickers

    def test_find_markets_no_volume_filter_when_zero(self, mock_client, config):
        """min_volume=0 skips the volume filter entirely."""
        config["filters"]["min_volume"] = 0
        mock_client.get_markets.return_value = {
            "markets": [
                {"ticker": "MENTION-A", "event_ticker": "EVT-A", "yes_bid": 50, "no_bid": 50, "volume": 0},
            ],
            "cursor": None,
        }
        mock_client.get_series.return_value = {"series": [{"ticker": "EVT"}]}

        strategy = MentionsStrategy(mock_client, config)

        with patch("src.strategies.mentions.time.time", return_value=1000000):
            markets = strategy.find_markets()

        assert len(markets) == 1
        assert markets[0]["ticker"] == "MENTION-A"

    def test_offset_mode_still_works(self, mock_client, config):
        """Legacy offset mode still produces a single order."""
        strategy = MentionsStrategy(mock_client, config)

        market = {
            "ticker": "MENTION-TEST",
            "_no_bid": 50,
        }

        orders = strategy.calculate_orders(market)

        assert len(orders) == 1
        assert orders[0].price_cents == 48  # 50 - 2
        assert orders[0].quantity == 15
