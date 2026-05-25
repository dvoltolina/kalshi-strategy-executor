# tests/test_order_budget.py
from unittest.mock import MagicMock

import pytest

from src.order_budget import (
    BudgetSnapshotError,
    OrderBudget,
    build_budget,
    order_notional_cents,
    snapshot_committed_cents,
)


class TestOrderBudgetArithmetic:
    def test_remaining_simple(self):
        b = OrderBudget(cap_cents=4500, committed_cents=1000)
        assert b.remaining_cents == 3500

    def test_remaining_decrements_with_reservations(self):
        b = OrderBudget(cap_cents=4500, committed_cents=1000)
        assert b.try_reserve(500) is True
        assert b.remaining_cents == 3000

    def test_reserve_at_exact_remaining(self):
        b = OrderBudget(cap_cents=1000, committed_cents=0)
        assert b.try_reserve(1000) is True
        assert b.remaining_cents == 0

    def test_reserve_over_remaining_fails(self):
        b = OrderBudget(cap_cents=1000, committed_cents=0)
        assert b.try_reserve(1001) is False
        assert b.reserved_cents == 0

    def test_reserve_non_positive_fails(self):
        b = OrderBudget(cap_cents=1000, committed_cents=0)
        assert b.try_reserve(0) is False
        assert b.try_reserve(-1) is False
        assert b.reserved_cents == 0

    def test_committed_above_cap_yields_zero_remaining(self):
        b = OrderBudget(cap_cents=1000, committed_cents=2000)
        assert b.remaining_cents == 0
        assert b.try_reserve(1) is False

    def test_release_returns_capacity(self):
        b = OrderBudget(cap_cents=1000, committed_cents=0)
        b.try_reserve(700)
        b.release(300)
        assert b.reserved_cents == 400
        assert b.remaining_cents == 600

    def test_release_clamps_at_zero(self):
        b = OrderBudget(cap_cents=1000, committed_cents=0)
        b.try_reserve(200)
        b.release(500)
        assert b.reserved_cents == 0

    def test_release_non_positive_is_noop(self):
        b = OrderBudget(cap_cents=1000, committed_cents=0)
        b.try_reserve(200)
        b.release(0)
        b.release(-5)
        assert b.reserved_cents == 200


class TestNoCapMode:
    def test_zero_cap_always_allows(self):
        b = OrderBudget(cap_cents=0, committed_cents=0)
        assert b.try_reserve(10**9) is True
        # reserved is not tracked in no-cap mode
        assert b.reserved_cents == 0

    def test_zero_cap_release_noop(self):
        b = OrderBudget(cap_cents=0, committed_cents=0)
        b.release(100)
        assert b.reserved_cents == 0

    def test_zero_cap_remaining_sentinel_is_large(self):
        b = OrderBudget(cap_cents=0, committed_cents=0)
        assert b.remaining_cents > 10**9


class TestReleaseCommitted:
    def test_release_committed_decreases_baseline(self):
        b = OrderBudget(cap_cents=4500, committed_cents=2000)
        b.release_committed(500)
        assert b.committed_cents == 1500
        assert b.remaining_cents == 3000

    def test_release_committed_clamps_at_zero(self):
        b = OrderBudget(cap_cents=4500, committed_cents=100)
        b.release_committed(500)
        assert b.committed_cents == 0

    def test_release_committed_no_cap_is_noop(self):
        b = OrderBudget(cap_cents=0, committed_cents=0)
        b.release_committed(500)
        assert b.committed_cents == 0


class TestOrderNotionalCents:
    def test_no_side_uses_no_price(self):
        order = {"side": "no", "remaining_count": 5, "no_price": 80}
        assert order_notional_cents(order) == 400

    def test_yes_side_uses_yes_price(self):
        order = {"side": "yes", "remaining_count": 3, "yes_price": 25}
        assert order_notional_cents(order) == 75

    def test_zero_remaining_returns_zero(self):
        order = {"side": "no", "remaining_count": 0, "no_price": 80}
        assert order_notional_cents(order) == 0

    def test_missing_price_falls_back_to_100c(self):
        order = {"side": "no", "remaining_count": 4}
        assert order_notional_cents(order) == 400


class TestSnapshotCommittedCents:
    def test_empty_account_yields_zero(self):
        client = MagicMock()
        client.get_positions.return_value = {"market_positions": [], "cursor": ""}
        client.get_orders.return_value = {"orders": [], "cursor": ""}
        assert snapshot_committed_cents(client) == 0

    def test_position_market_exposure_used_when_present(self):
        client = MagicMock()
        client.get_positions.return_value = {
            "market_positions": [
                {"ticker": "T1", "position": 10, "market_exposure": 850}
            ],
            "cursor": "",
        }
        client.get_orders.return_value = {"orders": [], "cursor": ""}
        assert snapshot_committed_cents(client) == 850

    def test_position_falls_back_to_count_times_100c(self):
        client = MagicMock()
        client.get_positions.return_value = {
            "market_positions": [{"ticker": "T1", "position": 10}],
            "cursor": "",
        }
        client.get_orders.return_value = {"orders": [], "cursor": ""}
        assert snapshot_committed_cents(client) == 1000

    def test_resting_orders_priced_by_side(self):
        client = MagicMock()
        client.get_positions.return_value = {"market_positions": [], "cursor": ""}
        client.get_orders.return_value = {
            "orders": [
                {"side": "no", "remaining_count": 5, "no_price": 80, "ticker": "A"},
                {"side": "yes", "remaining_count": 3, "yes_price": 25, "ticker": "B"},
            ],
            "cursor": "",
        }
        assert snapshot_committed_cents(client) == 400 + 75

    def test_positions_api_failure_raises_snapshot_error(self):
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("kalshi 503")
        with pytest.raises(BudgetSnapshotError, match="get_positions failed"):
            snapshot_committed_cents(client)

    def test_orders_api_failure_raises_snapshot_error(self):
        client = MagicMock()
        client.get_positions.return_value = {"market_positions": [], "cursor": ""}
        client.get_orders.side_effect = RuntimeError("kalshi timeout")
        with pytest.raises(BudgetSnapshotError, match="get_orders failed"):
            snapshot_committed_cents(client)


class TestBuildBudget:
    def test_no_cap_when_cap_usd_is_none(self):
        client = MagicMock()
        b = build_budget(client, None)
        assert b.cap_cents == 0
        # No Kalshi calls in no-cap mode
        client.get_positions.assert_not_called()
        client.get_orders.assert_not_called()

    def test_with_cap_queries_kalshi(self):
        client = MagicMock()
        client.get_positions.return_value = {"market_positions": [], "cursor": ""}
        client.get_orders.return_value = {"orders": [], "cursor": ""}
        b = build_budget(client, 45.0)
        assert b.cap_cents == 4500
        assert b.committed_cents == 0
        assert b.remaining_cents == 4500

    def test_snapshot_error_propagates(self):
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("boom")
        with pytest.raises(BudgetSnapshotError):
            build_budget(client, 45.0)

    def test_existing_exposure_above_cap_yields_zero_remaining(self):
        """If account already has resting+held exposure > cap, no new orders."""
        client = MagicMock()
        client.get_positions.return_value = {
            "market_positions": [{"position": 100, "market_exposure": 6000}],
            "cursor": "",
        }
        client.get_orders.return_value = {"orders": [], "cursor": ""}
        b = build_budget(client, 45.0)
        assert b.remaining_cents == 0
