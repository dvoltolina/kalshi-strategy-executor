# tests/test_reconciler.py
import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch

from src.database import Database
from src.kalshi_client import KalshiAPIError
from src.reconciler import Reconciler


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        database = Database(db_path)
        yield database
        database.close()


def _insert_order(db, client_order_id, order_id, status="pending"):
    """Helper to insert a test order."""
    db.record_order(
        client_order_id=client_order_id,
        order_id=order_id,
        strategy="mentions",
        ticker=f"MKT-{client_order_id}",
        side="no",
        action="buy",
        price_cents=60,
        quantity=50,
        status=status,
        market_snapshot={},
    )


class TestReconcileOrders:
    def test_no_pending_orders(self, db):
        client = MagicMock()
        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()
        assert stats == {"updated": 0, "unchanged": 0, "missing": 0, "errors": 0}
        client.get_order.assert_not_called()

    def test_resting_order_unchanged(self, db):
        _insert_order(db, "c1", "kalshi-1")
        client = MagicMock()
        client.get_order.return_value = {
            "order": {"status": "resting", "fill_count": 0, "initial_count": 50}
        }

        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()

        assert stats["unchanged"] == 1
        assert stats["updated"] == 0

        # DB status should still be pending
        row = db.conn.execute(
            "SELECT status FROM orders WHERE order_id = ?", ("kalshi-1",)
        ).fetchone()
        assert row["status"] == "pending"

    def test_executed_order_updated(self, db):
        _insert_order(db, "c1", "kalshi-1")
        client = MagicMock()
        client.get_order.return_value = {
            "order": {"status": "executed", "fill_count": 50, "initial_count": 50}
        }

        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()

        assert stats["updated"] == 1

        row = db.conn.execute(
            "SELECT status, filled_quantity FROM orders WHERE order_id = ?",
            ("kalshi-1",),
        ).fetchone()
        assert row["status"] == "filled"
        assert row["filled_quantity"] == 50

    def test_cancelled_order_updated(self, db):
        _insert_order(db, "c1", "kalshi-1")
        client = MagicMock()
        client.get_order.return_value = {
            "order": {"status": "canceled", "fill_count": 6, "initial_count": 50}
        }

        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()

        assert stats["updated"] == 1

        row = db.conn.execute(
            "SELECT status, filled_quantity FROM orders WHERE order_id = ?",
            ("kalshi-1",),
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["filled_quantity"] == 6

    def test_order_not_found_marked_cancelled(self, db):
        _insert_order(db, "c1", "kalshi-1")
        client = MagicMock()
        client.get_order.side_effect = KalshiAPIError(404, "not found")

        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()

        assert stats["missing"] == 1

        row = db.conn.execute(
            "SELECT status, filled_quantity FROM orders WHERE order_id = ?",
            ("kalshi-1",),
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["filled_quantity"] is None

    def test_api_error_counted(self, db):
        _insert_order(db, "c1", "kalshi-1")
        client = MagicMock()
        client.get_order.side_effect = KalshiAPIError(500, "internal error")

        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()

        assert stats["errors"] == 1

        # Status unchanged
        row = db.conn.execute(
            "SELECT status FROM orders WHERE order_id = ?", ("kalshi-1",)
        ).fetchone()
        assert row["status"] == "pending"

    def test_skips_already_cancelled_orders(self, db):
        _insert_order(db, "c1", "kalshi-1", status="cancelled")
        client = MagicMock()

        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()

        assert stats == {"updated": 0, "unchanged": 0, "missing": 0, "errors": 0}
        client.get_order.assert_not_called()

    def test_multiple_orders_mixed_statuses(self, db):
        _insert_order(db, "c1", "kalshi-1")
        _insert_order(db, "c2", "kalshi-2")
        _insert_order(db, "c3", "kalshi-3")

        client = MagicMock()

        def get_order_side_effect(order_id):
            if order_id == "kalshi-1":
                return {"order": {"status": "resting", "fill_count": 0, "initial_count": 50}}
            elif order_id == "kalshi-2":
                return {"order": {"status": "executed", "fill_count": 50, "initial_count": 50}}
            elif order_id == "kalshi-3":
                return {"order": {"status": "canceled", "fill_count": 10, "initial_count": 50}}

        client.get_order.side_effect = get_order_side_effect

        reconciler = Reconciler(client, db)
        stats = reconciler.reconcile_orders()

        assert stats["unchanged"] == 1
        assert stats["updated"] == 2


class TestReconcileSettlements:
    def test_delegates_to_settlement_checker(self, db):
        client = MagicMock()

        # Insert an order and a settled market
        _insert_order(db, "c1", "kalshi-1")
        client.get_market.return_value = {
            "market": {"status": "finalized", "result": "no"}
        }
        client.get_order.return_value = {
            "order": {"fill_count": 50, "remaining_count": 0}
        }

        reconciler = Reconciler(client, db)
        results = reconciler.reconcile_settlements()

        assert results["updated"] == 1
