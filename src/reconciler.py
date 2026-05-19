# src/reconciler.py
"""Startup reconciliation — syncs order and settlement state from Kalshi API to DB."""
import logging
from typing import Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from src.database import Database
    from src.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


class Reconciler:
    """Syncs order and settlement state on startup or after WS reconnect."""

    def __init__(self, client: "KalshiClient", db: "Database"):
        self.client = client
        self.db = db

    def reconcile_orders(self) -> Dict[str, int]:
        """Sync pending order state from Kalshi API to DB.

        For each order in our DB that's still pending/resting:
        - API says filled → update DB status + filled_quantity
        - API says cancelled → update DB status + fill_count
        - API says resting → no action (guard manages it)
        - API doesn't return it → mark cancelled with unknown fills

        Returns:
            Dict with updated, unchanged, missing, errors counts
        """
        stats = {"updated": 0, "unchanged": 0, "missing": 0, "errors": 0}

        pending_ids = self.db.get_pending_order_ids()
        if not pending_ids:
            logger.info("No pending orders to reconcile")
            return stats

        logger.info(f"Reconciling {len(pending_ids)} pending orders")

        for order_id in pending_ids:
            try:
                response = self.client.get_order(order_id)
                order_data = response.get("order", {})
                api_status = order_data.get("status", "")
                fill_count = order_data.get("fill_count", 0)
                initial_count = order_data.get("initial_count", 0)

                if api_status == "executed":
                    self.db.update_order_status(
                        order_id, "filled", filled_quantity=initial_count
                    )
                    logger.info(
                        f"  Reconciled {order_id}: filled ({initial_count} contracts)"
                    )
                    stats["updated"] += 1
                elif api_status in ("canceled", "cancelled"):
                    self.db.update_order_status(
                        order_id, "cancelled", filled_quantity=fill_count
                    )
                    logger.info(
                        f"  Reconciled {order_id}: cancelled ({fill_count} filled)"
                    )
                    stats["updated"] += 1
                elif api_status == "resting":
                    stats["unchanged"] += 1
                else:
                    logger.warning(
                        f"  Unknown API status for {order_id}: {api_status}"
                    )
                    stats["unchanged"] += 1

            except Exception as e:
                error_str = str(e).lower()
                if "not found" in error_str or "404" in error_str:
                    self.db.update_order_status(
                        order_id, "cancelled", filled_quantity=None
                    )
                    logger.warning(
                        f"  Order {order_id} not found on API, marked cancelled",
                        exc_info=True,
                    )
                    stats["missing"] += 1
                else:
                    logger.error(f"  Error reconciling {order_id}: {e}", exc_info=True)
                    stats["errors"] += 1

        logger.info(
            f"Reconciliation complete: {stats['updated']} updated, "
            f"{stats['unchanged']} unchanged, {stats['missing']} missing, "
            f"{stats['errors']} errors"
        )
        return stats

    def reconcile_settlements(self) -> Dict[str, int]:
        """Check all unsettled orders for settlement outcomes."""
        from src.settlement_checker import SettlementChecker

        checker = SettlementChecker(self.client, self.db)
        return checker.check_all()
