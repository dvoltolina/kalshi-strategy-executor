# tests/test_strategies/test_longshot.py
import pytest
from unittest.mock import MagicMock

from src.strategies.longshot import LongshotStrategy
from src.strategies.base import OrderParams


class TestLongshotStrategy:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def config(self):
        return {
            "name": "longshot",
            "filters": {
                "categories": ["Sports", "NFL"],
                "min_yes_price": 0.02,
                "max_yes_price": 0.19,
                "exclude_tickers_containing": ["MENTION"],
                "exclude_multivariate": True,
            },
            "order": {
                "side": "no",
                "contracts_per_market": 10,
                "pricing": {"mode": "at_bid"},
                "post_only": True,
            },
        }

    def test_find_markets_applies_filters(self, mock_client, config):
        mock_client.get_markets.return_value = {
            "markets": [
                {"ticker": "SPORTS-A", "event_ticker": "KXNFL-A", "yes_bid": 10, "no_bid": 90, "is_mve": False},
                {"ticker": "MENTION-B", "event_ticker": "KXMENTION-B", "yes_bid": 10, "no_bid": 90, "is_mve": False},
                {"ticker": "SPORTS-C", "event_ticker": "KXNFL-C", "yes_bid": 50, "no_bid": 50, "is_mve": False},
                {"ticker": "SPORTS-D", "event_ticker": "KXNFL-A", "yes_bid": 15, "no_bid": 85, "is_mve": False},
            ],
            "cursor": None,
        }
        # Mock series lookup: KXNFL is Sports, KXMENTION is not in Sports/NFL
        mock_client.get_series.side_effect = [
            {"series": [{"ticker": "KXNFL"}]},   # Sports
            {"series": []},                        # NFL (no extra series)
        ]

        strategy = LongshotStrategy(mock_client, config)
        markets = strategy.find_markets()

        assert len(markets) == 2
        tickers = {m["ticker"] for m in markets}
        assert tickers == {"SPORTS-A", "SPORTS-D"}

    def test_calculate_order_at_bid(self, mock_client, config):
        strategy = LongshotStrategy(mock_client, config)

        market = {
            "ticker": "TEST-MARKET",
            "event_ticker": "TEST-EVENT",
            "title": "Test Market",
            "_yes_bid": 0.15,
            "_no_bid": 85,
            "_category": "Sports",
            "yes_ask": 16,
            "no_ask": 86,
            "volume": 1000,
        }

        order = strategy.calculate_order(market)

        assert order is not None
        assert order.ticker == "TEST-MARKET"
        assert order.side == "no"
        assert order.action == "buy"
        assert order.price_cents == 85
        assert order.quantity == 10
        assert order.post_only is True
        assert order.market_snapshot["_category"] == "Sports"

    def test_calculate_order_skips_no_bid(self, mock_client, config):
        strategy = LongshotStrategy(mock_client, config)

        market = {"ticker": "TEST", "_yes_bid": 0.15}

        order = strategy.calculate_order(market)

        assert order is None
