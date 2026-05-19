# tests/test_database.py
import os
import tempfile
import pytest

from src.database import Database


class TestDatabaseInit:
    def test_creates_tables_on_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            cursor = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cursor.fetchall()}

            assert "orders" in tables
            assert "market_snapshots" in tables
            assert "settlements" in tables
            db.close()

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "subdir", "test.db")
            db = Database(db_path)
            assert os.path.exists(db_path)
            db.close()


class TestSchemaMigration:
    def test_adds_filled_quantity_to_existing_db(self):
        """Opening an old DB without filled_quantity column adds it via migration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")

            # Create a DB with the old schema (no filled_quantity)
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    order_id TEXT UNIQUE,
                    client_order_id TEXT UNIQUE NOT NULL,
                    strategy TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price_cents INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT INTO orders (client_order_id, order_id, strategy, ticker,
                    side, action, price_cents, quantity, status)
                VALUES ('old-order', 'kalshi-old', 'mentions', 'MKT-OLD',
                    'no', 'buy', 65, 50, 'pending')
            """)
            conn.commit()
            conn.close()

            # Reopen with Database class — migration should add the column
            db = Database(db_path)

            # Should be able to update with filled_quantity now
            db.update_order_status("kalshi-old", "cancelled", filled_quantity=6)

            row = db.conn.execute(
                "SELECT filled_quantity FROM orders WHERE order_id = ?",
                ("kalshi-old",),
            ).fetchone()
            assert row[0] == 6
            db.close()


class TestRecordOrder:
    def test_record_order_inserts_order_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            db.record_order(
                client_order_id="test-uuid-123",
                order_id="kalshi-order-456",
                strategy="longshot",
                ticker="TEST-MARKET",
                side="no",
                action="buy",
                price_cents=85,
                quantity=10,
                status="pending",
                market_snapshot={
                    "event_ticker": "TEST-EVENT",
                    "title": "Test Market Title",
                    "category": "Sports",
                    "yes_bid": 15,
                    "yes_ask": 16,
                    "no_bid": 84,
                    "no_ask": 85,
                    "volume": 1000,
                    "close_time": "2026-02-05T00:00:00Z",
                },
            )

            cursor = db.conn.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                ("test-uuid-123",)
            )
            order = cursor.fetchone()
            assert order is not None
            assert order["strategy"] == "longshot"
            assert order["ticker"] == "TEST-MARKET"
            assert order["price_cents"] == 85

            cursor = db.conn.execute(
                "SELECT * FROM market_snapshots WHERE order_id = ?",
                ("test-uuid-123",)
            )
            snapshot = cursor.fetchone()
            assert snapshot is not None
            assert snapshot["yes_bid"] == 15
            assert snapshot["category"] == "Sports"

            db.close()

    def test_record_order_with_dry_run_order_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            db.record_order(
                client_order_id="dry-run-uuid",
                order_id=None,
                strategy="mentions",
                ticker="MENTION-TEST",
                side="no",
                action="buy",
                price_cents=50,
                quantity=15,
                status="dry_run",
                market_snapshot={},
            )

            cursor = db.conn.execute(
                "SELECT order_id, status FROM orders WHERE client_order_id = ?",
                ("dry-run-uuid",)
            )
            order = cursor.fetchone()
            assert order["order_id"] is None
            assert order["status"] == "dry_run"

            db.close()


class TestSettlements:
    def _insert_test_order(self, db: Database, client_order_id: str, ticker: str):
        """Helper to insert a test order."""
        db.record_order(
            client_order_id=client_order_id,
            order_id=f"kalshi-{client_order_id}",
            strategy="test",
            ticker=ticker,
            side="no",
            action="buy",
            price_cents=85,
            quantity=10,
            status="filled",
            market_snapshot={"ticker": ticker},
        )

    def test_get_unsettled_orders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            self._insert_test_order(db, "order-1", "MARKET-A")
            self._insert_test_order(db, "order-2", "MARKET-B")
            self._insert_test_order(db, "order-3", "MARKET-C")

            db.record_settlement("order-2", "MARKET-B", "no", 15)

            unsettled = db.get_unsettled_orders()

            assert len(unsettled) == 2
            tickers = {o["ticker"] for o in unsettled}
            assert tickers == {"MARKET-A", "MARKET-C"}

            db.close()

    def test_record_settlement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            self._insert_test_order(db, "order-1", "MARKET-A")

            db.record_settlement("order-1", "MARKET-A", "no", 15)

            cursor = db.conn.execute(
                "SELECT * FROM settlements WHERE order_id = ?",
                ("order-1",)
            )
            settlement = cursor.fetchone()

            assert settlement is not None
            assert settlement["result"] == "no"
            assert settlement["pnl_cents"] == 15

            db.close()

    def test_get_unsettled_excludes_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            self._insert_test_order(db, "order-1", "MARKET-A")

            db.record_order(
                client_order_id="dry-run-1",
                order_id=None,
                strategy="test",
                ticker="MARKET-B",
                side="no",
                action="buy",
                price_cents=50,
                quantity=10,
                status="dry_run",
                market_snapshot={},
            )

            unsettled = db.get_unsettled_orders()

            assert len(unsettled) == 1
            assert unsettled[0]["ticker"] == "MARKET-A"

            db.close()

    def test_get_unsettled_excludes_cancelled_zero_fills(self):
        """Cancelled orders with 0 fills should be excluded from unsettled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            self._insert_test_order(db, "order-1", "MARKET-A")
            self._insert_test_order(db, "order-2", "MARKET-B")

            # Cancel order-1 with 0 fills
            db.update_order_status("kalshi-order-1", "cancelled", filled_quantity=0)

            unsettled = db.get_unsettled_orders()

            assert len(unsettled) == 1
            assert unsettled[0]["ticker"] == "MARKET-B"

            db.close()

    def test_get_unsettled_includes_cancelled_with_partial_fills(self):
        """Cancelled orders WITH fills should still appear in unsettled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            self._insert_test_order(db, "order-1", "MARKET-A")

            # Cancel with 6 fills
            db.update_order_status("kalshi-order-1", "cancelled", filled_quantity=6)

            unsettled = db.get_unsettled_orders()

            assert len(unsettled) == 1
            assert unsettled[0]["filled_quantity"] == 6

            db.close()

    def test_get_unsettled_includes_cancelled_without_filled_quantity(self):
        """Cancelled orders without filled_quantity (legacy) should still appear."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            self._insert_test_order(db, "order-1", "MARKET-A")

            # Cancel without setting filled_quantity (legacy behavior)
            db.update_order_status("kalshi-order-1", "cancelled")

            unsettled = db.get_unsettled_orders()

            # Should still appear (filled_quantity is NULL, COALESCE treats as non-zero)
            assert len(unsettled) == 1

            db.close()


class TestGetTradedTickers:
    def test_get_traded_tickers_pending_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # Add some orders
            for ticker in ["MARKET-A", "MARKET-B", "MARKET-C"]:
                db.record_order(
                    client_order_id=f"order-{ticker}",
                    order_id=f"kalshi-{ticker}",
                    strategy="mentions",
                    ticker=ticker,
                    side="no",
                    action="buy",
                    price_cents=50,
                    quantity=10,
                    status="filled",
                    market_snapshot={},
                )

            # Settle one of them
            db.record_settlement("order-MARKET-B", "MARKET-B", "no", 50)

            # Should only return unsettled tickers
            traded = db.get_traded_tickers(pending_only=True)
            assert traded == {"MARKET-A", "MARKET-C"}

            # Should return all tickers
            all_traded = db.get_traded_tickers(pending_only=False)
            assert all_traded == {"MARKET-A", "MARKET-B", "MARKET-C"}

            db.close()

    def test_get_traded_tickers_by_strategy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            db.record_order(
                client_order_id="order-1",
                order_id="kalshi-1",
                strategy="mentions",
                ticker="MENTION-MARKET",
                side="no",
                action="buy",
                price_cents=50,
                quantity=10,
                status="filled",
                market_snapshot={},
            )
            db.record_order(
                client_order_id="order-2",
                order_id="kalshi-2",
                strategy="longshot",
                ticker="LONGSHOT-MARKET",
                side="no",
                action="buy",
                price_cents=85,
                quantity=10,
                status="filled",
                market_snapshot={},
            )

            mentions_traded = db.get_traded_tickers(strategy="mentions")
            assert mentions_traded == {"MENTION-MARKET"}

            longshot_traded = db.get_traded_tickers(strategy="longshot")
            assert longshot_traded == {"LONGSHOT-MARKET"}

            db.close()


class TestPerformanceSummary:
    def _setup_orders_with_settlements(self, db: Database):
        """Setup test data with mixed outcomes."""
        # Winning orders (bought NO, NO won)
        for i in range(3):
            db.record_order(
                client_order_id=f"win-{i}",
                order_id=f"kalshi-win-{i}",
                strategy="longshot",
                ticker=f"WIN-{i}",
                side="no",
                action="buy",
                price_cents=85,
                quantity=10,
                status="filled",
                market_snapshot={},
            )
            db.record_settlement(f"win-{i}", f"WIN-{i}", "no", 15)

        # Losing order (bought NO, YES won)
        db.record_order(
            client_order_id="lose-1",
            order_id="kalshi-lose-1",
            strategy="longshot",
            ticker="LOSE-1",
            side="no",
            action="buy",
            price_cents=85,
            quantity=10,
            status="filled",
            market_snapshot={},
        )
        db.record_settlement("lose-1", "LOSE-1", "yes", -85)

        # Different strategy
        db.record_order(
            client_order_id="mentions-1",
            order_id="kalshi-mentions-1",
            strategy="mentions",
            ticker="MENTION-1",
            side="no",
            action="buy",
            price_cents=50,
            quantity=10,
            status="filled",
            market_snapshot={},
        )
        db.record_settlement("mentions-1", "MENTION-1", "no", 50)

    def test_get_performance_summary_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)
            self._setup_orders_with_settlements(db)

            summary = db.get_performance_summary()

            assert summary["total_orders"] == 5
            assert summary["settled_orders"] == 5
            assert summary["wins"] == 4
            assert summary["losses"] == 1
            assert summary["total_pnl_cents"] == 10  # 3*15 + (-85) + 50 = 10

            db.close()

    def test_get_performance_summary_by_strategy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)
            self._setup_orders_with_settlements(db)

            summary = db.get_performance_summary(strategy="longshot")

            assert summary["total_orders"] == 4
            assert summary["wins"] == 3
            assert summary["losses"] == 1
            assert summary["total_pnl_cents"] == -40  # 3*15 + (-85) = -40

            db.close()


class TestGetRecentOrderTickers:
    def test_recent_order_within_window(self):
        """Order placed just now should appear in recent tickers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            db.record_order(
                client_order_id="recent-1",
                order_id="kalshi-recent-1",
                strategy="mentions",
                ticker="MKT-A",
                side="no",
                action="buy",
                price_cents=70,
                quantity=500,
                status="pending",
                market_snapshot={},
            )

            recent = db.get_recent_order_tickers("mentions", within_hours=2)
            assert "MKT-A" in recent

            db.close()

    def test_old_order_outside_window(self):
        """Order placed >2h ago should NOT appear in recent tickers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            db.record_order(
                client_order_id="old-1",
                order_id="kalshi-old-1",
                strategy="mentions",
                ticker="MKT-A",
                side="no",
                action="buy",
                price_cents=70,
                quantity=500,
                status="pending",
                market_snapshot={},
            )

            # Backdate the order to 3 hours ago
            db.conn.execute(
                "UPDATE orders SET created_at = datetime('now', '-3 hours') "
                "WHERE client_order_id = 'old-1'"
            )
            db.conn.commit()

            recent = db.get_recent_order_tickers("mentions", within_hours=2)
            assert "MKT-A" not in recent

            db.close()

    def test_filters_by_strategy(self):
        """Only returns tickers for the specified strategy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            db.record_order(
                client_order_id="mentions-1",
                order_id="kalshi-m1",
                strategy="mentions",
                ticker="MKT-A",
                side="no",
                action="buy",
                price_cents=70,
                quantity=500,
                status="pending",
                market_snapshot={},
            )
            db.record_order(
                client_order_id="longshot-1",
                order_id="kalshi-l1",
                strategy="longshot",
                ticker="MKT-B",
                side="no",
                action="buy",
                price_cents=85,
                quantity=10,
                status="pending",
                market_snapshot={},
            )

            recent = db.get_recent_order_tickers("mentions", within_hours=2)
            assert recent == {"MKT-A"}

            db.close()

    def test_empty_when_no_orders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            recent = db.get_recent_order_tickers("mentions", within_hours=2)
            assert recent == set()

            db.close()
