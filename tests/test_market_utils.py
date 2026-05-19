# tests/test_market_utils.py
import pytest
from unittest.mock import MagicMock

from src.market_utils import (
    filter_by_yes_price,
    filter_by_no_price,
    filter_by_category,
    filter_by_series_category,
    exclude_multivariate,
    exclude_tickers_containing,
)


class TestFilterByYesPrice:
    def test_filters_markets_in_range(self):
        markets = [
            {"ticker": "A", "yes_bid": 10},   # 0.10 - in range
            {"ticker": "B", "yes_bid": 50},   # 0.50 - too high
            {"ticker": "C", "yes_bid": 5},    # 0.05 - in range
            {"ticker": "D", "yes_bid": 1},    # 0.01 - too low
        ]

        result = filter_by_yes_price(markets, min_price=0.02, max_price=0.19)

        assert len(result) == 2
        tickers = {m["ticker"] for m in result}
        assert tickers == {"A", "C"}

    def test_handles_missing_yes_bid(self):
        markets = [
            {"ticker": "A", "yes_bid": 10},
            {"ticker": "B"},
        ]

        result = filter_by_yes_price(markets, min_price=0.02, max_price=0.19)

        assert len(result) == 1
        assert result[0]["ticker"] == "A"


class TestFilterByNoPrice:
    def test_filters_markets_in_range(self):
        markets = [
            {"ticker": "A", "no_bid": 85},  # 0.85 - in range
            {"ticker": "B", "no_bid": 50},  # 0.50 - too low
            {"ticker": "C", "no_bid": 90},  # 0.90 - in range
            {"ticker": "D", "no_bid": 99},  # 0.99 - too high
        ]

        result = filter_by_no_price(markets, min_price=0.81, max_price=0.98)

        assert len(result) == 2
        tickers = {m["ticker"] for m in result}
        assert tickers == {"A", "C"}

    def test_handles_missing_no_bid(self):
        markets = [
            {"ticker": "A", "no_bid": 85},
            {"ticker": "B"},  # No no_bid
            {"ticker": "C", "no_bid": 0},  # Zero no_bid
        ]

        result = filter_by_no_price(markets, min_price=0.81, max_price=0.98)

        assert len(result) == 1
        assert result[0]["ticker"] == "A"

    def test_adds_no_bid_annotation(self):
        """Should add _no_bid in cents to filtered markets."""
        markets = [{"ticker": "A", "no_bid": 85}]

        result = filter_by_no_price(markets, min_price=0.81, max_price=0.98)

        assert result[0]["_no_bid"] == 85


class TestExcludeMultivariate:
    def test_excludes_mve_markets(self):
        markets = [
            {"ticker": "A", "is_mve": False},
            {"ticker": "B", "is_mve": True},
            {"ticker": "C"},
        ]

        result = exclude_multivariate(markets)

        assert len(result) == 2
        tickers = {m["ticker"] for m in result}
        assert tickers == {"A", "C"}


class TestExcludeTickersContaining:
    def test_excludes_matching_tickers(self):
        markets = [
            {"ticker": "SPORTS-NFL-123"},
            {"ticker": "MENTION-TRUMP"},
            {"ticker": "MENTION-BIDEN"},
            {"ticker": "WEATHER-NYC"},
        ]

        result = exclude_tickers_containing(markets, ["MENTION"])

        assert len(result) == 2
        tickers = {m["ticker"] for m in result}
        assert tickers == {"SPORTS-NFL-123", "WEATHER-NYC"}

    def test_case_insensitive(self):
        markets = [
            {"ticker": "mention-test"},
            {"ticker": "MENTION-TEST"},
            {"ticker": "Mention-Test"},
            {"ticker": "OTHER"},
        ]

        result = exclude_tickers_containing(markets, ["MENTION"])

        assert len(result) == 1
        assert result[0]["ticker"] == "OTHER"


class TestFilterByCategory:
    def test_filters_by_category(self):
        mock_client = MagicMock()
        mock_client.get_event.side_effect = [
            {"event": {"category": "Sports"}},
            {"event": {"category": "Politics"}},
            {"event": {"category": "NFL"}},
        ]

        markets = [
            {"ticker": "A", "event_ticker": "EVT-A"},
            {"ticker": "B", "event_ticker": "EVT-B"},
            {"ticker": "C", "event_ticker": "EVT-C"},
        ]

        result = filter_by_category(markets, ["Sports", "NFL"], mock_client)

        assert len(result) == 2
        tickers = {m["ticker"] for m in result}
        assert tickers == {"A", "C"}

    def test_caches_event_lookups(self):
        mock_client = MagicMock()
        mock_client.get_event.return_value = {"event": {"category": "Sports"}}

        markets = [
            {"ticker": "A", "event_ticker": "EVT-SHARED"},
            {"ticker": "B", "event_ticker": "EVT-SHARED"},
        ]

        event_cache = {}
        result = filter_by_category(markets, ["Sports"], mock_client, event_cache)

        assert mock_client.get_event.call_count == 1
        assert len(result) == 2

    def test_skips_markets_without_event_ticker(self):
        """Markets without event_ticker should be skipped."""
        mock_client = MagicMock()
        mock_client.get_event.return_value = {"event": {"category": "Sports"}}

        markets = [
            {"ticker": "A", "event_ticker": "EVT-A"},
            {"ticker": "B"},  # No event_ticker
            {"ticker": "C", "event_ticker": ""},  # Empty event_ticker
        ]

        result = filter_by_category(markets, ["Sports"], mock_client)

        # Only market A should be processed
        assert mock_client.get_event.call_count == 1
        assert len(result) == 1
        assert result[0]["ticker"] == "A"

    def test_handles_empty_event_response(self):
        """Handle case where API returns empty event data."""
        mock_client = MagicMock()
        mock_client.get_event.return_value = {"event": {}}  # No category

        markets = [
            {"ticker": "A", "event_ticker": "EVT-A"},
        ]

        result = filter_by_category(markets, ["Sports"], mock_client)

        # Empty category doesn't match "Sports"
        assert len(result) == 0


class TestFilterBySeriesCategory:
    def test_matches_by_series_prefix(self):
        mock_client = MagicMock()
        mock_client.get_series.return_value = {
            "series": [{"ticker": "KXMENTION"}, {"ticker": "KXSPORTS"}]
        }

        markets = [
            {"ticker": "A", "event_ticker": "KXMENTION-26MAR25"},
            {"ticker": "B", "event_ticker": "KXOTHER-26MAR25"},
        ]

        result = filter_by_series_category(markets, ["Mentions"], mock_client)

        assert len(result) == 1
        assert result[0]["ticker"] == "A"

    def test_handles_null_series_from_category(self):
        """API sometimes returns {'series': null} — should not crash."""
        mock_client = MagicMock()
        mock_client.get_series.return_value = {"series": None}

        markets = [{"ticker": "A", "event_ticker": "KXMENTION-26MAR25"}]

        result = filter_by_series_category(markets, ["Mentions"], mock_client)

        assert result == []

    def test_handles_null_series_from_tags(self):
        """tags query returns {'series': null} — should not crash."""
        mock_client = MagicMock()
        mock_client.get_series.side_effect = [
            {"series": [{"ticker": "KXMENTION"}]},  # category query
            {"series": None},  # tags query
        ]

        markets = [
            {"ticker": "A", "event_ticker": "KXMENTION-26MAR25"},
        ]

        result = filter_by_series_category(
            markets, ["Mentions"], mock_client, tags=["Mentions"]
        )

        assert len(result) == 1
        assert result[0]["ticker"] == "A"
