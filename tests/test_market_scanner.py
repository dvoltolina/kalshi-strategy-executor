# tests/test_market_scanner.py
import pytest
from unittest.mock import MagicMock


def test_filter_markets_by_category():
    """Markets are filtered by sports category."""
    from src.market_scanner import MarketScanner

    mock_client = MagicMock()
    scanner = MarketScanner(
        client=mock_client,
        sports_categories=["NFL", "NBA"],
        min_yes_price=0.02,
        max_yes_price=0.19,
        min_no_price=0.81,
        max_no_price=0.98,
    )

    markets = [
        {"ticker": "NFL-GAME-1", "event_ticker": "NFL-EVENT-1"},
        {"ticker": "POLITICS-1", "event_ticker": "POL-EVENT-1"},
        {"ticker": "NBA-GAME-1", "event_ticker": "NBA-EVENT-1"},
    ]

    mock_client.get_event.side_effect = [
        {"event": {"category": "NFL"}},
        {"event": {"category": "Politics"}},
        {"event": {"category": "NBA"}},
    ]

    filtered = scanner._filter_by_category(markets)

    assert len(filtered) == 2
    assert filtered[0]["ticker"] == "NFL-GAME-1"
    assert filtered[1]["ticker"] == "NBA-GAME-1"


def test_filter_mention_tickers():
    """MENTION tickers are excluded."""
    from src.market_scanner import MarketScanner

    mock_client = MagicMock()
    scanner = MarketScanner(
        client=mock_client,
        sports_categories=["NFL"],
        min_yes_price=0.02,
        max_yes_price=0.19,
        min_no_price=0.81,
        max_no_price=0.98,
    )

    markets = [
        {"ticker": "NFL-GAME-1"},
        {"ticker": "NFL-MENTION-BRADY"},
        {"ticker": "NFL-GAME-2"},
        {"ticker": "NBA-ANNOUNCERMENTION-1"},
    ]

    filtered = scanner._filter_mention_tickers(markets)

    assert len(filtered) == 2
    assert filtered[0]["ticker"] == "NFL-GAME-1"
    assert filtered[1]["ticker"] == "NFL-GAME-2"


def test_filter_multivariate():
    """Multivariate event markets are excluded."""
    from src.market_scanner import MarketScanner

    mock_client = MagicMock()
    scanner = MarketScanner(
        client=mock_client,
        sports_categories=["NFL"],
        min_yes_price=0.02,
        max_yes_price=0.19,
        min_no_price=0.81,
        max_no_price=0.98,
    )

    markets = [
        {"ticker": "NFL-GAME-1", "is_mve": False},
        {"ticker": "NFL-MVP-MAHOMES", "is_mve": True},  # MVE - should be excluded
        {"ticker": "NFL-GAME-2"},  # No is_mve field - should be included
        {"ticker": "NFL-FIRSTSCORE-CHIEFS", "is_mve": True},  # MVE - excluded
    ]

    filtered = scanner._filter_multivariate(markets)

    assert len(filtered) == 2
    assert filtered[0]["ticker"] == "NFL-GAME-1"
    assert filtered[1]["ticker"] == "NFL-GAME-2"


def test_filter_by_price():
    """Markets filtered by yes price range."""
    from src.market_scanner import MarketScanner

    mock_client = MagicMock()
    scanner = MarketScanner(
        client=mock_client,
        sports_categories=["NFL"],
        min_yes_price=0.02,
        max_yes_price=0.19,
        min_no_price=0.81,
        max_no_price=0.98,
    )

    markets = [
        {"ticker": "M1", "yes_bid": 0.01},  # Too low
        {"ticker": "M2", "yes_bid": 0.05},  # In range
        {"ticker": "M3", "yes_bid": 0.19},  # In range (boundary)
        {"ticker": "M4", "yes_bid": 0.50},  # Too high
        {"ticker": "M5", "yes_bid": 15},    # 15 cents, in range
    ]

    filtered = scanner._filter_by_price(markets)

    assert len(filtered) == 3
    tickers = [m["ticker"] for m in filtered]
    assert "M2" in tickers
    assert "M3" in tickers
    assert "M5" in tickers


def test_filter_has_no_bid():
    """Markets with no_bid outside range are excluded."""
    from src.market_scanner import MarketScanner

    mock_client = MagicMock()
    scanner = MarketScanner(
        client=mock_client,
        sports_categories=["NFL"],
        min_yes_price=0.02,
        max_yes_price=0.19,
        min_no_price=0.81,
        max_no_price=0.98,
    )

    markets = [
        {"ticker": "M1", "no_bid": 85},       # 85c - in range (81-98)
        {"ticker": "M2", "no_bid": None},     # No value
        {"ticker": "M3", "no_bid": 0},        # Zero
        {"ticker": "M4", "no_bid_dollars": {"value": "0.90"}},  # 90c - in range
        {"ticker": "M5", "no_bid": 75},       # 75c - below range
        {"ticker": "M6", "no_bid": 99},       # 99c - above range
    ]

    filtered = scanner._filter_has_no_bid(markets)

    assert len(filtered) == 2
    tickers = [m["ticker"] for m in filtered]
    assert "M1" in tickers
    assert "M4" in tickers
    # M5 (75c) and M6 (99c) should be excluded as out of range


def test_scan_integration(mocker):
    """Full scan returns qualifying markets."""
    from src.market_scanner import MarketScanner

    mock_client = MagicMock()

    mock_client.get_markets.return_value = {
        "markets": [
            {
                "ticker": "NFL-CHIEFS-WIN",
                "event_ticker": "NFL-SUPERBOWL",
                "title": "Chiefs Win Super Bowl",
                "yes_bid": 0.15,
                "no_bid": 85,
            },
            {
                "ticker": "NFL-MENTION-MAHOMES",
                "event_ticker": "NFL-MENTIONS",
                "title": "Mahomes Mentioned",
                "yes_bid": 0.10,
                "no_bid": 90,
            },
            {
                "ticker": "POLITICS-ELECTION",
                "event_ticker": "POL-2024",
                "title": "Election Result",
                "yes_bid": 0.15,
                "no_bid": 85,
            },
            {
                "ticker": "NFL-MVP-MAHOMES",
                "event_ticker": "NFL-MVP",
                "title": "Mahomes MVP",
                "yes_bid": 0.10,
                "no_bid": 90,
                "is_mve": True,  # Multivariate - should be excluded
            },
        ],
        "cursor": None,
    }

    # Only 2 markets pass MVE + MENTION filters, so only 2 events are fetched
    mock_client.get_event.side_effect = [
        {"event": {"category": "NFL"}},       # NFL-SUPERBOWL
        {"event": {"category": "Politics"}},  # POL-2024
    ]

    scanner = MarketScanner(
        client=mock_client,
        sports_categories=["NFL", "NBA"],
        min_yes_price=0.02,
        max_yes_price=0.19,
        min_no_price=0.81,
        max_no_price=0.98,
    )

    results = scanner.scan()

    # Only NFL-CHIEFS-WIN should qualify (MVE excluded, MENTION excluded, Politics excluded)
    assert len(results) == 1
    assert results[0].ticker == "NFL-CHIEFS-WIN"
    assert results[0].yes_bid == 0.15
    assert results[0].no_bid == 85
