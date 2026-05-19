# src/settlement_checker.py
"""Settlement checker for tracking order outcomes."""
import logging
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from src.kalshi_client import KalshiClient
    from src.database import Database

logger = logging.getLogger(__name__)


class SettlementChecker:
    """Checks and records settlement outcomes for orders."""

    def __init__(self, client: "KalshiClient", db: "Database"):
        """Initialize checker.

        Args:
            client: Kalshi API client
            db: Database instance
        """
        self.client = client
        self.db = db

    def check_all(self) -> Dict[str, int]:
        """Check all unsettled orders and update database.

        Returns:
            Dict with updated, still_pending, errors counts
        """
        results = {"updated": 0, "still_pending": 0, "errors": 0}

        unsettled = self.db.get_unsettled_orders()
        if not unsettled:
            logger.info("No unsettled orders to check")
            return results

        logger.info(f"Checking {len(unsettled)} unsettled orders")

        # Group by ticker to minimize API calls
        tickers = set(o["ticker"] for o in unsettled)
        market_results: Dict[str, Dict[str, Any]] = {}

        for ticker in tickers:
            try:
                response = self.client.get_market(ticker)
                market_results[ticker] = response.get("market", {})
            except Exception as e:
                logger.error(f"Error fetching market {ticker}: {e}", exc_info=True)
                results["errors"] += 1

        # Process each order
        for order in unsettled:
            ticker = order["ticker"]
            market = market_results.get(ticker)

            if not market:
                continue

            market_status = market.get("status", "")
            if market_status not in ("settled", "finalized"):
                results["still_pending"] += 1
                continue

            # Resolve actual fill count before calculating P&L
            # Pending orders may not have filled_quantity set yet
            if order.get("filled_quantity") is None and order.get("order_id"):
                try:
                    api_order = self.client.get_order(order["order_id"])
                    order_data = api_order.get("order", {})
                    fill_count = order_data.get(
                        "fill_count", order["quantity"]
                    )
                    order["filled_quantity"] = fill_count
                except Exception:
                    logger.warning(
                        f"Could not fetch fill count for order "
                        f"{order['order_id']}, assuming full fill",
                        exc_info=True,
                    )

            # Calculate P&L
            winner = market.get("result")  # 'yes', 'no', or None (voided)

            if winner is None:
                # Market voided/cancelled — Kalshi refunds, no P&L
                pnl = 0
                logger.info(f"Settled {ticker}: voided (no winner), PnL=0c")
            else:
                pnl = self._calculate_pnl(order, winner)
                logger.info(f"Settled {ticker}: {winner} won, PnL={pnl}c")

            # Record settlement
            self.db.record_settlement(
                order_id=order["client_order_id"],
                ticker=ticker,
                result=winner or "voided",
                pnl_cents=pnl,
            )

            # Update stale pending orders with resolved fill count
            if order.get("status") == "pending":
                filled = order.get("filled_quantity", order["quantity"])
                self.db.update_order_status(
                    order["order_id"], "filled", filled_quantity=filled
                )

            results["updated"] += 1

        return results

    def _calculate_pnl(self, order: Dict[str, Any], winner: str) -> int:
        """Calculate profit/loss for an order.

        Uses filled_quantity when available (partial fills from cancelled orders),
        falling back to the original quantity for fully executed orders.

        Args:
            order: Order dict with side, price_cents, quantity, filled_quantity
            winner: 'yes' or 'no' - which side won

        Returns:
            P&L in cents (positive = profit, negative = loss)
        """
        side = order["side"]
        price_cents = order["price_cents"]
        filled = order.get("filled_quantity")
        status = order.get("status", "")
        # Cancelled orders with unknown fills: treat as 0 (no position to settle)
        if filled is None and status == "cancelled":
            quantity = 0
        else:
            quantity = filled if filled is not None else order["quantity"]

        if side == winner:
            # We win: payout is 100c per contract minus what we paid
            pnl_per_contract = 100 - price_cents
        else:
            # We lose: lose what we paid
            pnl_per_contract = -price_cents

        return pnl_per_contract * quantity
