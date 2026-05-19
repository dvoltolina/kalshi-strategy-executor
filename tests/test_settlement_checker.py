# tests/test_settlement_checker.py
import os
import tempfile
import pytest
from unittest.mock import MagicMock

from src.settlement_checker import SettlementChecker
from src.database import Database


class TestSettlementChecker:
    @pytest.fixture
    def db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            database = Database(db_path)
            yield database
            database.close()

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        # Default: API reports full fill for any order lookup
        client.get_order.return_value = {
            "order": {"fill_count": 10, "remaining_count": 0}
        }
        return client

    def _insert_order(self, db, client_order_id, ticker, side="no", price_cents=85):
        db.record_order(
            client_order_id=client_order_id,
            order_id=f"kalshi-{client_order_id}",
            strategy="test",
            ticker=ticker,
            side=side,
            action="buy",
            price_cents=price_cents,
            quantity=10,
            status="filled",
            market_snapshot={},
        )

    def test_check_all_updates_settled_markets(self, db, mock_client):
        self._insert_order(db, "order-1", "MARKET-A", price_cents=85)
        self._insert_order(db, "order-2", "MARKET-B", price_cents=50)

        # Market A settled as NO (we win)
        # Market B settled as YES (we lose)
        mock_client.get_market.side_effect = [
            {"market": {"status": "settled", "result": "no"}},
            {"market": {"status": "settled", "result": "yes"}},
        ]

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["updated"] == 2
        assert results["still_pending"] == 0

        # Verify settlements recorded
        unsettled = db.get_unsettled_orders()
        assert len(unsettled) == 0

    def test_check_all_skips_unsettled_markets(self, db, mock_client):
        self._insert_order(db, "order-1", "MARKET-A")

        mock_client.get_market.return_value = {
            "market": {"status": "open"}
        }

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["updated"] == 0
        assert results["still_pending"] == 1

    def test_calculates_pnl_correctly_for_no_win(self, db, mock_client):
        """Bought NO at 85c, NO wins -> profit = 100 - 85 = 15c per contract."""
        self._insert_order(db, "order-1", "MARKET-A", side="no", price_cents=85)

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "no"}
        }

        checker = SettlementChecker(mock_client, db)
        checker.check_all()

        # Check PnL
        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("order-1",)
        )
        settlement = cursor.fetchone()
        # Profit = (100 - 85) * 10 contracts = 150 cents
        assert settlement["pnl_cents"] == 150

    def test_calculates_pnl_correctly_for_no_loss(self, db, mock_client):
        """Bought NO at 85c, YES wins -> loss = 85c per contract."""
        self._insert_order(db, "order-1", "MARKET-A", side="no", price_cents=85)

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "yes"}
        }

        checker = SettlementChecker(mock_client, db)
        checker.check_all()

        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("order-1",)
        )
        settlement = cursor.fetchone()
        # Loss = -85 * 10 contracts = -850 cents
        assert settlement["pnl_cents"] == -850

    def test_calculates_pnl_correctly_for_yes_win(self, db, mock_client):
        """Bought YES at 15c, YES wins -> profit = 100 - 15 = 85c per contract."""
        self._insert_order(db, "order-1", "MARKET-A", side="yes", price_cents=15)

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "yes"}
        }

        checker = SettlementChecker(mock_client, db)
        checker.check_all()

        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("order-1",)
        )
        settlement = cursor.fetchone()
        # Profit = (100 - 15) * 10 contracts = 850 cents
        assert settlement["pnl_cents"] == 850

    def test_calculates_pnl_correctly_for_yes_loss(self, db, mock_client):
        """Bought YES at 15c, NO wins -> loss = 15c per contract."""
        self._insert_order(db, "order-1", "MARKET-A", side="yes", price_cents=15)

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "no"}
        }

        checker = SettlementChecker(mock_client, db)
        checker.check_all()

        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("order-1",)
        )
        settlement = cursor.fetchone()
        # Loss = -15 * 10 contracts = -150 cents
        assert settlement["pnl_cents"] == -150

    def test_check_all_returns_early_when_no_unsettled(self, db, mock_client):
        """No API calls made when there are no unsettled orders."""
        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results == {"updated": 0, "still_pending": 0, "errors": 0}
        mock_client.get_market.assert_not_called()

    def test_check_all_handles_api_errors(self, db, mock_client):
        """Errors fetching market data are counted and logged."""
        self._insert_order(db, "order-1", "MARKET-A")

        mock_client.get_market.side_effect = Exception("API timeout")

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["errors"] == 1
        assert results["updated"] == 0
        assert results["still_pending"] == 0

    def test_check_all_continues_after_single_api_error(self, db, mock_client):
        """One API error doesn't stop processing of other markets."""
        self._insert_order(db, "order-1", "MARKET-A")
        self._insert_order(db, "order-2", "MARKET-B")

        # First call fails, second succeeds
        mock_client.get_market.side_effect = [
            Exception("API timeout"),
            {"market": {"status": "settled", "result": "no"}},
        ]

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["errors"] == 1
        assert results["updated"] == 1

    def test_cancelled_order_zero_fills_excluded(self, db, mock_client):
        """Cancelled orders with 0 fills should not appear in settlement check."""
        self._insert_order(db, "order-1", "MARKET-A")

        # Cancel with 0 fills
        db.update_order_status("kalshi-order-1", "cancelled", filled_quantity=0)

        unsettled = db.get_unsettled_orders()
        assert len(unsettled) == 0

        # Settlement checker should have nothing to do
        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()
        assert results["updated"] == 0
        mock_client.get_market.assert_not_called()

    def test_cancelled_order_partial_fills_uses_filled_quantity(self, db, mock_client):
        """Cancelled order with partial fills: P&L based on filled_quantity, not quantity."""
        # Order for 50 contracts
        db.record_order(
            client_order_id="partial-1",
            order_id="kalshi-partial-1",
            strategy="test",
            ticker="MARKET-P",
            side="no",
            action="buy",
            price_cents=60,
            quantity=50,
            status="pending",
            market_snapshot={},
        )

        # Cancel with 6 fills
        db.update_order_status("kalshi-partial-1", "cancelled", filled_quantity=6)

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "no"}
        }

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["updated"] == 1

        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("partial-1",)
        )
        settlement = cursor.fetchone()
        # P&L = (100 - 60) * 6 = 240 cents (NOT 2000 from 50 contracts)
        assert settlement["pnl_cents"] == 240

    def test_voided_market_records_zero_pnl(self, db, mock_client):
        """Voided/cancelled market (result=None) records PnL as 0."""
        self._insert_order(db, "order-1", "MARKET-V", side="no", price_cents=70)

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": None}
        }

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["updated"] == 1

        cursor = db.conn.execute(
            "SELECT pnl_cents, result FROM settlements WHERE order_id = ?",
            ("order-1",)
        )
        settlement = cursor.fetchone()
        assert settlement["pnl_cents"] == 0
        assert settlement["result"] == "voided"

    def test_normal_order_without_filled_quantity_uses_quantity(self, db, mock_client):
        """Orders without filled_quantity (pre-existing) still use original quantity."""
        self._insert_order(db, "order-1", "MARKET-A", side="no", price_cents=85)

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "no"}
        }

        checker = SettlementChecker(mock_client, db)
        checker.check_all()

        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("order-1",)
        )
        settlement = cursor.fetchone()
        # P&L = (100 - 85) * 10 = 150 (uses quantity=10 since filled_quantity is NULL)
        assert settlement["pnl_cents"] == 150

    def test_pending_order_status_updated_on_settlement(self, db, mock_client):
        """Pending orders get status updated to filled when market settles."""
        db.record_order(
            client_order_id="pending-1",
            order_id="kalshi-pending-1",
            strategy="test",
            ticker="MARKET-P",
            side="no",
            action="buy",
            price_cents=60,
            quantity=10,
            status="pending",
            market_snapshot={},
        )

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "no"}
        }

        checker = SettlementChecker(mock_client, db)
        checker.check_all()

        # Order status should be updated to filled
        cursor = db.conn.execute(
            "SELECT status, filled_quantity FROM orders WHERE order_id = ?",
            ("kalshi-pending-1",)
        )
        order = cursor.fetchone()
        assert order["status"] == "filled"
        assert order["filled_quantity"] == 10

    def test_cancelled_order_null_filled_quantity_queries_api(self, db, mock_client):
        """Cancelled order with NULL filled_quantity queries API for actual fills."""
        db.record_order(
            client_order_id="cancel-null-1",
            order_id="kalshi-cancel-null-1",
            strategy="test",
            ticker="MARKET-C",
            side="no",
            action="buy",
            price_cents=60,
            quantity=50,
            status="pending",
            market_snapshot={},
        )

        # Cancel WITHOUT recording filled_quantity (simulates API error during cancel)
        db.conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE order_id = ?",
            ("kalshi-cancel-null-1",),
        )
        db.conn.commit()

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "no"}
        }
        # API confirms 0 fills
        mock_client.get_order.return_value = {
            "order": {"fill_count": 0, "remaining_count": 50}
        }

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["updated"] == 1
        mock_client.get_order.assert_called_with("kalshi-cancel-null-1")

        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("cancel-null-1",)
        )
        settlement = cursor.fetchone()
        # API confirmed 0 fills → 0 P&L
        assert settlement["pnl_cents"] == 0

    def test_cancelled_order_null_filled_api_failure_assumes_zero(self, db, mock_client):
        """If API fails for cancelled order with NULL fills, treat as 0 (conservative)."""
        db.record_order(
            client_order_id="cancel-fail-1",
            order_id="kalshi-cancel-fail-1",
            strategy="test",
            ticker="MARKET-CF",
            side="no",
            action="buy",
            price_cents=60,
            quantity=50,
            status="pending",
            market_snapshot={},
        )

        db.conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE order_id = ?",
            ("kalshi-cancel-fail-1",),
        )
        db.conn.commit()

        mock_client.get_market.return_value = {
            "market": {"status": "settled", "result": "no"}
        }
        mock_client.get_order.side_effect = Exception("API error")

        checker = SettlementChecker(mock_client, db)
        results = checker.check_all()

        assert results["updated"] == 1

        cursor = db.conn.execute(
            "SELECT pnl_cents FROM settlements WHERE order_id = ?",
            ("cancel-fail-1",)
        )
        settlement = cursor.fetchone()
        # API failed, filled_quantity still None, cancelled → 0 P&L (conservative)
        assert settlement["pnl_cents"] == 0
