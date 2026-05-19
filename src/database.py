# src/database.py
"""SQLite database for order tracking and performance analysis."""
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


class Database:
    """SQLite database for persistent order tracking."""

    def __init__(self, path: str):
        """Initialize database, creating tables if needed."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                order_id TEXT UNIQUE,
                client_order_id TEXT UNIQUE NOT NULL,
                strategy TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                filled_quantity INTEGER,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY,
                order_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                event_ticker TEXT,
                title TEXT,
                category TEXT,
                yes_bid INTEGER,
                yes_ask INTEGER,
                no_bid INTEGER,
                no_ask INTEGER,
                volume INTEGER,
                open_interest INTEGER,
                volume_24h INTEGER,
                last_price INTEGER,
                liquidity INTEGER,
                close_time TIMESTAMP,
                snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(client_order_id)
            );

            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY,
                order_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                result TEXT,
                pnl_cents INTEGER,
                settled_at TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(client_order_id)
            );

            CREATE TABLE IF NOT EXISTS event_estimates (
                event_ticker TEXT PRIMARY KEY,
                estimated_start_utc TEXT NOT NULL,
                confidence TEXT NOT NULL,
                reasoning TEXT,
                event_title TEXT,
                cached_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
            CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_settlements_order_id ON settlements(order_id);
        """)
        self.conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add columns that may be missing from older databases."""
        # filled_quantity added for partial fill tracking on cancel
        try:
            self.conn.execute("SELECT filled_quantity FROM orders LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute(
                "ALTER TABLE orders ADD COLUMN filled_quantity INTEGER"
            )
            self.conn.commit()

        # market_snapshots: additional fields for debugging
        for col in ["open_interest", "volume_24h", "last_price", "liquidity"]:
            try:
                self.conn.execute(f"SELECT {col} FROM market_snapshots LIMIT 1")
            except sqlite3.OperationalError:
                self.conn.execute(
                    f"ALTER TABLE market_snapshots ADD COLUMN {col} INTEGER"
                )
                self.conn.commit()

        # event_title added for audit trail on event estimates
        try:
            self.conn.execute("SELECT event_title FROM event_estimates LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute(
                "ALTER TABLE event_estimates ADD COLUMN event_title TEXT"
            )
            self.conn.commit()

    def record_order(
        self,
        client_order_id: str,
        order_id: Optional[str],
        strategy: str,
        ticker: str,
        side: str,
        action: str,
        price_cents: int,
        quantity: int,
        status: str,
        market_snapshot: Dict[str, Any],
    ) -> None:
        """Record an order and its market snapshot.

        Args:
            client_order_id: Our UUID for the order
            order_id: Kalshi's order ID (None for dry runs)
            strategy: Strategy name that placed the order
            ticker: Market ticker
            side: 'yes' or 'no'
            action: 'buy' or 'sell'
            price_cents: Limit price in cents
            quantity: Number of contracts
            status: Order status
            market_snapshot: Market state at order time
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO orders (
                    client_order_id, order_id, strategy, ticker, side,
                    action, price_cents, quantity, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id, order_id, strategy, ticker, side,
                    action, price_cents, quantity, status,
                ),
            )

            self.conn.execute(
                """
                INSERT INTO market_snapshots (
                    order_id, ticker, event_ticker, title, category,
                    yes_bid, yes_ask, no_bid, no_ask, volume,
                    open_interest, volume_24h, last_price, liquidity,
                    close_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    ticker,
                    market_snapshot.get("event_ticker"),
                    market_snapshot.get("title"),
                    market_snapshot.get("category"),
                    market_snapshot.get("yes_bid"),
                    market_snapshot.get("yes_ask"),
                    market_snapshot.get("no_bid"),
                    market_snapshot.get("no_ask"),
                    market_snapshot.get("volume"),
                    market_snapshot.get("open_interest"),
                    market_snapshot.get("volume_24h"),
                    market_snapshot.get("last_price"),
                    market_snapshot.get("liquidity"),
                    market_snapshot.get("close_time"),
                ),
            )

    def get_unsettled_orders(self) -> List[Dict[str, Any]]:
        """Get orders that don't have settlement records.

        Cancelled orders with 0 fills are excluded (no position exists).
        For partially filled cancelled orders, filled_quantity is returned
        so the settlement checker can use the correct effective quantity.

        Returns:
            List of order dicts with client_order_id, ticker, side,
            price_cents, quantity, filled_quantity
        """
        cursor = self.conn.execute(
            """
            SELECT o.client_order_id, o.order_id, o.ticker, o.side,
                   o.price_cents, o.quantity, o.filled_quantity, o.status
            FROM orders o
            LEFT JOIN settlements s ON o.client_order_id = s.order_id
            WHERE s.id IS NULL
              AND o.status != 'dry_run'
              AND NOT (o.status = 'cancelled' AND o.filled_quantity IS NOT NULL
                       AND o.filled_quantity = 0)
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def record_settlement(
        self,
        order_id: str,
        ticker: str,
        result: str,
        pnl_cents: int,
    ) -> None:
        """Record settlement outcome for an order.

        Args:
            order_id: The client_order_id of the order
            ticker: Market ticker
            result: 'yes' or 'no' (which side won)
            pnl_cents: Profit/loss in cents
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO settlements (order_id, ticker, result, pnl_cents, settled_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (order_id, ticker, result, pnl_cents),
            )

    def get_performance_summary(self, strategy: Optional[str] = None) -> Dict[str, Any]:
        """Get aggregated performance statistics.

        Args:
            strategy: Filter by strategy name, or None for all strategies

        Returns:
            Dict with total_orders, settled_orders, wins, losses, total_pnl_cents
        """
        strategy_filter = ""
        params: tuple = ()
        if strategy:
            strategy_filter = "WHERE o.strategy = ?"
            params = (strategy,)

        cursor = self.conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT o.client_order_id) as total_orders,
                COUNT(DISTINCT s.order_id) as settled_orders,
                SUM(CASE WHEN s.pnl_cents > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN s.pnl_cents < 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(s.pnl_cents), 0) as total_pnl_cents
            FROM orders o
            LEFT JOIN settlements s ON o.client_order_id = s.order_id
            {strategy_filter}
            """,
            params,
        )
        row = cursor.fetchone()

        return {
            "total_orders": row["total_orders"] or 0,
            "settled_orders": row["settled_orders"] or 0,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "total_pnl_cents": row["total_pnl_cents"] or 0,
        }

    def get_settled_orders_detail(self, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get per-order detail for all settled orders.

        Returns rows with: ticker, title, side, price_cents, quantity,
        filled_quantity, result, pnl_cents, settled_at, strategy.
        """
        strategy_filter = ""
        params: tuple = ()
        if strategy:
            strategy_filter = "AND o.strategy = ?"
            params = (strategy,)

        cursor = self.conn.execute(
            f"""
            SELECT
                o.ticker,
                o.side,
                o.price_cents,
                o.quantity,
                o.filled_quantity,
                o.strategy,
                s.result,
                s.pnl_cents,
                s.settled_at,
                ms.title
            FROM settlements s
            JOIN orders o ON s.order_id = o.client_order_id
            LEFT JOIN market_snapshots ms ON o.client_order_id = ms.order_id
            WHERE 1=1 {strategy_filter}
            ORDER BY s.settled_at DESC
            """,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_traded_tickers(self, strategy: Optional[str] = None, pending_only: bool = True) -> set:
        """Get set of tickers that have already been traded.

        Args:
            strategy: Filter by strategy name, or None for all strategies
            pending_only: If True, only return tickers with unsettled orders

        Returns:
            Set of ticker strings
        """
        query = "SELECT DISTINCT ticker FROM orders WHERE 1=1"
        params: list = []

        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)

        if pending_only:
            query += """ AND client_order_id NOT IN (
                SELECT order_id FROM settlements
            )"""

        cursor = self.conn.execute(query, params)
        return {row["ticker"] for row in cursor.fetchall()}

    def update_order_status(
        self, order_id: str, status: str, filled_quantity: Optional[int] = None
    ) -> None:
        """Update the status of an order by its Kalshi order ID.

        Args:
            order_id: Kalshi's order ID
            status: New status (e.g., 'cancelled')
            filled_quantity: Number of contracts filled (optional)
        """
        with self.conn:
            if filled_quantity is not None:
                self.conn.execute(
                    "UPDATE orders SET status = ?, filled_quantity = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                    (status, filled_quantity, order_id),
                )
            else:
                self.conn.execute(
                    "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE order_id = ?",
                    (status, order_id),
                )

    def get_committed_contracts(self, strategy: str) -> Dict[str, int]:
        """Get total committed contracts per ticker for a strategy.

        Committed = resting/pending quantity + filled quantity + partial fills
        from cancelled orders. Excludes settled orders (position closed).

        Returns:
            Dict mapping ticker -> total committed contracts
        """
        cursor = self.conn.execute(
            """
            SELECT ticker, SUM(
                CASE
                    WHEN status = 'cancelled' AND filled_quantity IS NOT NULL
                        THEN filled_quantity
                    WHEN status NOT IN ('cancelled', 'dry_run', 'settled')
                        THEN quantity
                    ELSE 0
                END
            ) as committed
            FROM orders
            WHERE strategy = ?
              AND client_order_id NOT IN (SELECT order_id FROM settlements)
            GROUP BY ticker
            """,
            (strategy,),
        )
        return {row["ticker"]: row["committed"] for row in cursor.fetchall()}

    def get_pending_order_ids(
        self, strategy: Optional[str] = None, exclude_action: Optional[str] = None
    ) -> set:
        """Get Kalshi order IDs for pending (unsettled) orders.

        Args:
            strategy: Strategy name to filter by, or None for all strategies
            exclude_action: Exclude orders with this action (e.g., 'reprice')

        Returns:
            Set of Kalshi order_id strings
        """
        query = """
            SELECT order_id FROM orders
            WHERE order_id IS NOT NULL
              AND status NOT IN ('cancelled', 'settled', 'dry_run')
              AND client_order_id NOT IN (
                  SELECT order_id FROM settlements
              )
        """
        params: list = []
        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)
        if exclude_action:
            query += " AND action != ?"
            params.append(exclude_action)

        cursor = self.conn.execute(query, tuple(params))
        return {row["order_id"] for row in cursor.fetchall()}

    def save_event_estimate(
        self,
        event_ticker: str,
        estimated_start_utc: str,
        confidence: str,
        reasoning: str,
        cached_at: str,
        event_title: str = "",
    ) -> None:
        """Save or update an event start time estimate.

        Args:
            event_ticker: Event ticker identifier
            estimated_start_utc: ISO 8601 UTC datetime string
            confidence: 'high', 'medium', or 'low'
            reasoning: Explanation for the estimate
            cached_at: ISO 8601 UTC datetime when estimate was created
            event_title: Human-readable event title for audit trail
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO event_estimates
                    (event_ticker, estimated_start_utc, confidence, reasoning,
                     event_title, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_ticker, estimated_start_utc, confidence, reasoning,
                 event_title, cached_at),
            )

    def load_event_estimates(self) -> List[Dict[str, str]]:
        """Load all event estimates from database.

        Returns:
            List of dicts with event_ticker, estimated_start_utc, confidence,
            reasoning, event_title, cached_at
        """
        cursor = self.conn.execute(
            "SELECT event_ticker, estimated_start_utc, confidence, reasoning, "
            "event_title, cached_at FROM event_estimates"
        )
        return [dict(row) for row in cursor.fetchall()]

    def cleanup_old_estimates(self, before_utc: str) -> int:
        """Delete estimates for events that started before the given time.

        Args:
            before_utc: ISO 8601 UTC datetime; estimates with
                estimated_start_utc before this are deleted

        Returns:
            Number of rows deleted
        """
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM event_estimates WHERE estimated_start_utc < ?",
                (before_utc,),
            )
            return cursor.rowcount

    def get_recent_order_tickers(self, strategy: str, within_hours: int) -> set:
        """Get tickers that had an order placed within the last N hours.

        Args:
            strategy: Strategy name to filter by
            within_hours: Number of hours to look back

        Returns:
            Set of ticker strings with recent orders
        """
        cursor = self.conn.execute(
            """
            SELECT DISTINCT ticker FROM orders
            WHERE strategy = ?
              AND created_at > datetime('now', ?)
            """,
            (strategy, f"-{within_hours} hours"),
        )
        return {row["ticker"] for row in cursor.fetchall()}

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()
