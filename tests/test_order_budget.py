# tests/test_order_budget.py
import pytest

from src.order_budget import OrderBudget


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
