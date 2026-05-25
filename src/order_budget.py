# src/order_budget.py
"""Account-level dollar exposure cap for live trading.

OrderBudget is the single arithmetic primitive enforcing
MAX_TOTAL_NOTIONAL_USD across both the continuous runner and the
one-shot CLI path. It is injected into OrderPlacer; every call to
place_order_from_params asks the budget to reserve its notional
before reaching Kalshi.

The class is pure-Python with no I/O. It is constructed per trade
tick with a fresh snapshot of committed exposure (held positions +
resting orders) fetched from Kalshi by ``snapshot_committed_cents``,
then mutated in-memory as orders are placed during that tick.
"""
from dataclasses import dataclass
import logging
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


@dataclass
class OrderBudget:
    """In-memory tracker for account-level outstanding $ exposure.

    Attributes:
        cap_cents: Hard ceiling in cents. ``0`` means "no cap" — used
            only when DRY_RUN=true and MAX_TOTAL_NOTIONAL_USD is unset.
        committed_cents: Snapshot at construction time of cents already
            tied up in held positions + unfilled resting orders.
        reserved_cents: Incremental reservations made during this tick
            via :meth:`try_reserve`.
    """

    cap_cents: int
    committed_cents: int
    reserved_cents: int = 0

    @property
    def remaining_cents(self) -> int:
        """Cents still available for new orders this tick."""
        if self.cap_cents == 0:
            return 10**12  # effectively unlimited (sentinel for no-cap mode)
        return max(0, self.cap_cents - self.committed_cents - self.reserved_cents)

    def try_reserve(self, notional_cents: int) -> bool:
        """Reserve ``notional_cents`` if available. Returns success."""
        if notional_cents <= 0:
            return False
        if self.cap_cents == 0:
            return True
        if notional_cents > self.remaining_cents:
            return False
        self.reserved_cents += notional_cents
        return True

    def release(self, notional_cents: int) -> None:
        """Refund a previously reserved amount (e.g. after a live API failure)."""
        if notional_cents <= 0 or self.cap_cents == 0:
            return
        self.reserved_cents = max(0, self.reserved_cents - notional_cents)

    def release_committed(self, notional_cents: int) -> None:
        """Decrease the baseline ``committed_cents`` (e.g. after cancelling an
        order that was counted in the snapshot)."""
        if notional_cents <= 0 or self.cap_cents == 0:
            return
        self.committed_cents = max(0, self.committed_cents - notional_cents)


class BudgetSnapshotError(Exception):
    """Raised when we cannot construct a reliable budget snapshot.

    Treated as a fail-closed condition: the runner should abort the tick
    rather than place orders with unknown remaining capacity.
    """


def snapshot_committed_cents(client: "KalshiClient") -> int:
    """Sum cents tied up in held positions + unfilled resting orders.

    Raises:
        BudgetSnapshotError: if any Kalshi paginate call fails. Treating
            this as fail-closed prevents the runner from over-deploying
            on top of an unknown account state.
    """
    total = 0

    cursor = None
    while True:
        try:
            result = client.get_positions(
                limit=100, cursor=cursor, count_filter="position"
            )
        except Exception as e:
            raise BudgetSnapshotError(f"get_positions failed: {e}") from e
        for pos in result.get("market_positions", []):
            count = abs(pos.get("position", 0))
            if count <= 0:
                continue
            exposure = pos.get("market_exposure")
            if isinstance(exposure, (int, float)) and exposure > 0:
                total += int(exposure)
            else:
                total += count * 100  # conservative fallback: $1/contract
        cursor = result.get("cursor")
        if not cursor or not result.get("market_positions"):
            break

    cursor = None
    while True:
        try:
            result = client.get_orders(status="resting", limit=100, cursor=cursor)
        except Exception as e:
            raise BudgetSnapshotError(f"get_orders failed: {e}") from e
        for order in result.get("orders", []):
            remaining = order.get("remaining_count", 0)
            if remaining <= 0:
                continue
            side = order.get("side", "")
            price = order.get("no_price" if side == "no" else "yes_price")
            if not isinstance(price, (int, float)) or price <= 0:
                price = 100  # conservative fallback
            total += remaining * int(price)
        cursor = result.get("cursor")
        if not cursor or not result.get("orders"):
            break

    return total


def build_budget(client: "KalshiClient", cap_usd: Optional[float]) -> "OrderBudget":
    """Construct an OrderBudget for one trade tick or one-shot run.

    ``cap_usd is None`` yields a no-cap budget (dry-run convenience).
    Otherwise fetches a fresh exposure snapshot and logs the result.

    Raises:
        BudgetSnapshotError: see :func:`snapshot_committed_cents`.
    """
    if cap_usd is None:
        return OrderBudget(cap_cents=0, committed_cents=0)

    committed_cents = snapshot_committed_cents(client)
    cap_cents = int(round(cap_usd * 100))
    budget = OrderBudget(cap_cents=cap_cents, committed_cents=committed_cents)
    logger.info(
        f"Budget: cap=${cap_usd:.2f}, committed=${committed_cents/100:.2f}, "
        f"remaining=${budget.remaining_cents/100:.2f}"
    )
    if budget.remaining_cents == 0:
        msg = (
            f"WARNING: Budget exhausted at snapshot (cap=${cap_usd:.2f}, "
            f"committed=${committed_cents/100:.2f}); no new orders will "
            f"be placed this cycle."
        )
        logger.warning(msg)
        print(msg)
    return budget


def order_notional_cents(order: Any) -> int:
    """Approximate dollar notional (in cents) of a Kalshi order dict.

    Used by reprice when refunding committed_cents after cancellation.
    Mirrors the logic in :func:`snapshot_committed_cents`.
    """
    remaining = order.get("remaining_count", 0) if isinstance(order, dict) else 0
    if remaining <= 0:
        return 0
    side = order.get("side", "")
    price = order.get("no_price" if side == "no" else "yes_price")
    if not isinstance(price, (int, float)) or price <= 0:
        price = 100
    return remaining * int(price)
