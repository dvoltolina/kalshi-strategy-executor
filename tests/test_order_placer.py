# tests/test_order_placer.py
import pytest
from unittest.mock import MagicMock, patch

import requests

from src.strategies.base import OrderParams
from src.kalshi_client import KalshiAPIError
from src.order_budget import OrderBudget
from src.order_placer import OrderPlacer, _is_retryable


def test_place_order_success():
    """Order is placed with correct parameters."""
    from src.order_placer import OrderPlacer
    from src.market_scanner import QualifyingMarket

    mock_client = MagicMock()
    mock_client.create_order.return_value = {
        "order": {"order_id": "ord-123", "status": "open"}
    }

    placer = OrderPlacer(
        client=mock_client,
        contract_count=10,
        dry_run=False,
    )

    market = QualifyingMarket(
        ticker="NFL-TEST",
        event_ticker="NFL-EVENT",
        title="Test Market",
        yes_bid=0.15,
        no_bid=85,
        category="NFL",
    )

    result = placer.place_order(market)

    assert result.success is True
    assert result.order_id == "ord-123"
    mock_client.create_order.assert_called_once()
    call_kwargs = mock_client.create_order.call_args[1]
    assert call_kwargs["ticker"] == "NFL-TEST"
    assert call_kwargs["side"] == "no"
    assert call_kwargs["action"] == "buy"
    assert call_kwargs["count"] == 10
    assert call_kwargs["no_price"] == 85


def test_place_order_dry_run():
    """Dry run mode doesn't call API."""
    from src.order_placer import OrderPlacer
    from src.market_scanner import QualifyingMarket

    mock_client = MagicMock()

    placer = OrderPlacer(
        client=mock_client,
        contract_count=10,
        dry_run=True,
    )

    market = QualifyingMarket(
        ticker="NFL-TEST",
        event_ticker="NFL-EVENT",
        title="Test Market",
        yes_bid=0.15,
        no_bid=85,
        category="NFL",
    )

    result = placer.place_order(market)

    assert result.success is True
    assert result.dry_run is True
    assert "dry-run" in result.order_id
    mock_client.create_order.assert_not_called()


def test_place_order_api_error():
    """API errors are handled gracefully."""
    from src.order_placer import OrderPlacer
    from src.market_scanner import QualifyingMarket
    from src.kalshi_client import KalshiAPIError

    mock_client = MagicMock()
    mock_client.create_order.side_effect = KalshiAPIError(400, "Insufficient balance")

    placer = OrderPlacer(
        client=mock_client,
        contract_count=10,
        dry_run=False,
    )

    market = QualifyingMarket(
        ticker="NFL-TEST",
        event_ticker="NFL-EVENT",
        title="Test Market",
        yes_bid=0.15,
        no_bid=85,
        category="NFL",
    )

    result = placer.place_order(market)

    assert result.success is False
    assert "Insufficient balance" in result.error


class TestOrderPlacerWithOrderParams:
    def test_place_order_from_params(self):
        from src.order_placer import OrderPlacer

        mock_client = MagicMock()
        mock_client.create_order.return_value = {
            "order": {"order_id": "test-order-123"}
        }

        placer = OrderPlacer(client=mock_client, dry_run=False)

        params = OrderParams(
            ticker="TEST-MARKET",
            side="no",
            action="buy",
            price_cents=85,
            quantity=10,
            post_only=True,
            market_snapshot={"yes_bid": 15},
        )

        result = placer.place_order_from_params(params, strategy="longshot")

        assert result.success is True
        assert result.order_id == "test-order-123"

        mock_client.create_order.assert_called_once()
        call_kwargs = mock_client.create_order.call_args[1]
        assert call_kwargs["ticker"] == "TEST-MARKET"
        assert call_kwargs["no_price"] == 85
        assert call_kwargs["post_only"] is True

    def test_place_order_dry_run_returns_params(self):
        from src.order_placer import OrderPlacer

        mock_client = MagicMock()

        placer = OrderPlacer(client=mock_client, dry_run=True)

        params = OrderParams(
            ticker="TEST-MARKET",
            side="no",
            action="buy",
            price_cents=85,
            quantity=10,
            post_only=True,
            market_snapshot={},
        )

        result = placer.place_order_from_params(params, strategy="mentions")

        assert result.success is True
        assert result.dry_run is True
        mock_client.create_order.assert_not_called()


class TestRetryLogic:
    """Tests for exponential backoff retry on transient errors."""

    def test_is_retryable_rate_limit(self):
        assert _is_retryable(KalshiAPIError(429, "Rate limited")) is True

    def test_is_retryable_server_errors(self):
        for code in (500, 502, 503, 504):
            assert _is_retryable(KalshiAPIError(code, "Server error")) is True

    def test_is_not_retryable_client_errors(self):
        for code in (400, 401, 403, 404, 422):
            assert _is_retryable(KalshiAPIError(code, "Client error")) is False

    def test_is_retryable_connection_error(self):
        assert _is_retryable(requests.ConnectionError()) is True

    def test_is_retryable_timeout(self):
        assert _is_retryable(requests.Timeout()) is True

    def test_is_not_retryable_generic_exception(self):
        assert _is_retryable(ValueError("bad")) is False

    @patch("src.order_placer.time.sleep")
    def test_retry_succeeds_after_transient_failure(self, mock_sleep):
        """Order succeeds on second attempt after a 429."""
        from src.market_scanner import QualifyingMarket

        mock_client = MagicMock()
        mock_client.create_order.side_effect = [
            KalshiAPIError(429, "Rate limited"),
            {"order": {"order_id": "ord-retry-ok"}},
        ]

        placer = OrderPlacer(
            client=mock_client,
            contract_count=10,
            dry_run=False,
            max_retries=3,
            retry_base_delay=1.0,
        )

        market = QualifyingMarket(
            ticker="RETRY-TEST",
            event_ticker="RETRY-EVENT",
            title="Retry Market",
            yes_bid=0.10,
            no_bid=90,
            category="TEST",
        )

        result = placer.place_order(market)

        assert result.success is True
        assert result.order_id == "ord-retry-ok"
        assert mock_client.create_order.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    @patch("src.order_placer.time.sleep")
    def test_retry_exhausted_returns_failure(self, mock_sleep):
        """All retries exhausted returns failure result."""
        from src.market_scanner import QualifyingMarket

        mock_client = MagicMock()
        mock_client.create_order.side_effect = KalshiAPIError(503, "Service unavailable")

        placer = OrderPlacer(
            client=mock_client,
            contract_count=10,
            dry_run=False,
            max_retries=3,
            retry_base_delay=0.1,
        )

        market = QualifyingMarket(
            ticker="RETRY-FAIL",
            event_ticker="RETRY-EVENT",
            title="Retry Market",
            yes_bid=0.10,
            no_bid=90,
            category="TEST",
        )

        result = placer.place_order(market)

        assert result.success is False
        assert "Service unavailable" in result.error
        assert mock_client.create_order.call_count == 3
        # Backoff: 0.1s, 0.2s (2 sleeps for 3 attempts)
        assert mock_sleep.call_count == 2

    @patch("src.order_placer.time.sleep")
    def test_no_retry_on_client_error(self, mock_sleep):
        """Client errors (400) fail immediately without retrying."""
        from src.market_scanner import QualifyingMarket

        mock_client = MagicMock()
        mock_client.create_order.side_effect = KalshiAPIError(400, "Insufficient balance")

        placer = OrderPlacer(
            client=mock_client,
            contract_count=10,
            dry_run=False,
            max_retries=3,
        )

        market = QualifyingMarket(
            ticker="NO-RETRY",
            event_ticker="NO-RETRY-EVENT",
            title="No Retry Market",
            yes_bid=0.10,
            no_bid=90,
            category="TEST",
        )

        result = placer.place_order(market)

        assert result.success is False
        assert mock_client.create_order.call_count == 1
        mock_sleep.assert_not_called()

    @patch("src.order_placer.time.sleep")
    def test_retry_exponential_backoff_delays(self, mock_sleep):
        """Backoff delays double each attempt."""
        from src.market_scanner import QualifyingMarket

        mock_client = MagicMock()
        mock_client.create_order.side_effect = [
            KalshiAPIError(429, "Rate limited"),
            KalshiAPIError(429, "Rate limited"),
            {"order": {"order_id": "ord-backoff-ok"}},
        ]

        placer = OrderPlacer(
            client=mock_client,
            contract_count=10,
            dry_run=False,
            max_retries=3,
            retry_base_delay=2.0,
        )

        market = QualifyingMarket(
            ticker="BACKOFF-TEST",
            event_ticker="BACKOFF-EVENT",
            title="Backoff Market",
            yes_bid=0.10,
            no_bid=90,
            category="TEST",
        )

        result = placer.place_order(market)

        assert result.success is True
        assert mock_sleep.call_args_list[0][0][0] == 2.0   # 2 * 2^0
        assert mock_sleep.call_args_list[1][0][0] == 4.0   # 2 * 2^1

class TestOrderPlacerBudgetEnforcement:
    """Budget reservation + release semantics inside place_order_from_params."""

    def _params(self, qty=10, price=85):
        return OrderParams(
            ticker="BUDGET-TEST",
            side="no",
            action="buy",
            price_cents=price,
            quantity=qty,
            post_only=True,
            market_snapshot={"yes_bid": 15},
        )

    def test_budget_rejects_when_over_cap(self):
        mock_client = MagicMock()
        budget = OrderBudget(cap_cents=500, committed_cents=0)  # $5 cap
        placer = OrderPlacer(client=mock_client, dry_run=False, budget=budget)

        # 10 contracts × 85c = $8.50 > $5
        result = placer.place_order_from_params(self._params(), strategy="mentions")

        assert result.success is False
        assert result.error == "account notional cap reached"
        mock_client.create_order.assert_not_called()
        assert budget.reserved_cents == 0

    def test_budget_allows_when_under_cap_and_reserves(self):
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"order": {"order_id": "ord-ok"}}
        budget = OrderBudget(cap_cents=2000, committed_cents=0)  # $20 cap
        placer = OrderPlacer(client=mock_client, dry_run=False, budget=budget)

        result = placer.place_order_from_params(self._params(), strategy="mentions")

        assert result.success is True
        assert budget.reserved_cents == 850

    def test_budget_releases_on_kalshi_api_error(self):
        mock_client = MagicMock()
        mock_client.create_order.side_effect = KalshiAPIError(400, "Insufficient balance")
        budget = OrderBudget(cap_cents=2000, committed_cents=0)
        placer = OrderPlacer(client=mock_client, dry_run=False, budget=budget)

        result = placer.place_order_from_params(self._params(), strategy="mentions")

        assert result.success is False
        # Reservation returned to budget so next attempt has full headroom.
        assert budget.reserved_cents == 0
        assert budget.remaining_cents == 2000

    def test_budget_releases_on_unexpected_exception(self):
        mock_client = MagicMock()
        mock_client.create_order.side_effect = RuntimeError("boom")
        budget = OrderBudget(cap_cents=2000, committed_cents=0)
        placer = OrderPlacer(client=mock_client, dry_run=False, budget=budget)

        result = placer.place_order_from_params(self._params(), strategy="mentions")

        assert result.success is False
        assert budget.reserved_cents == 0

    def test_dry_run_with_budget_reserves_but_does_not_release(self):
        """Dry-run simulates a resting order, so the reservation stays."""
        mock_client = MagicMock()
        budget = OrderBudget(cap_cents=2000, committed_cents=0)
        placer = OrderPlacer(client=mock_client, dry_run=True, budget=budget)

        result = placer.place_order_from_params(self._params(), strategy="mentions")

        assert result.success is True
        assert result.dry_run is True
        assert budget.reserved_cents == 850
        mock_client.create_order.assert_not_called()

    def test_no_budget_falls_back_to_previous_behavior(self):
        """budget=None means no cap check; existing call sites stay green."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"order": {"order_id": "ord-ok"}}
        placer = OrderPlacer(client=mock_client, dry_run=False, budget=None)

        result = placer.place_order_from_params(self._params(), strategy="mentions")

        assert result.success is True
        mock_client.create_order.assert_called_once()

    def test_budget_exhausts_across_multiple_orders(self):
        """Sequential orders drain budget; first few succeed, then reject."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"order": {"order_id": "ord-ok"}}
        budget = OrderBudget(cap_cents=1700, committed_cents=0)  # exactly $17
        placer = OrderPlacer(client=mock_client, dry_run=False, budget=budget)

        # Two $8.50 orders fit ($17 cap); third must reject.
        r1 = placer.place_order_from_params(self._params(), strategy="mentions")
        r2 = placer.place_order_from_params(self._params(), strategy="mentions")
        r3 = placer.place_order_from_params(self._params(), strategy="mentions")

        assert r1.success is True
        assert r2.success is True
        assert r3.success is False
        assert r3.error == "account notional cap reached"
        assert mock_client.create_order.call_count == 2


class TestRetryWithParams:
    @patch("src.order_placer.time.sleep")
    def test_retry_works_with_place_order_from_params(self, mock_sleep):
        """Retry also works via place_order_from_params."""
        mock_client = MagicMock()
        mock_client.create_order.side_effect = [
            KalshiAPIError(502, "Bad gateway"),
            {"order": {"order_id": "ord-params-retry"}},
        ]

        placer = OrderPlacer(
            client=mock_client,
            dry_run=False,
            max_retries=3,
            retry_base_delay=1.0,
        )

        params = OrderParams(
            ticker="PARAMS-RETRY",
            side="no",
            action="buy",
            price_cents=85,
            quantity=10,
            post_only=False,
            market_snapshot={},
        )

        result = placer.place_order_from_params(params, strategy="mentions")

        assert result.success is True
        assert result.order_id == "ord-params-retry"
        assert mock_client.create_order.call_count == 2
        mock_sleep.assert_called_once_with(1.0)
