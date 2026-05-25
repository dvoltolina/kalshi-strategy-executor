# src/order_budget.py
"""Account-level dollar exposure cap for live trading.

OrderBudget is the single arithmetic primitive enforcing
MAX_TOTAL_NOTIONAL_USD across both the continuous runner and the
one-shot CLI path. It is injected into OrderPlacer; every call to
place_order_from_params asks the budget to reserve its notional
before reaching Kalshi.

The class is pure-Python with no I/O. It is constructed per trade
tick with a fresh snapshot of committed exposure (held positions +
resting orders) fetched from Kalshi by the runner, then mutated
in-memory as orders are placed during that tick.
"""
from dataclasses import dataclass


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
