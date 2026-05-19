# src/strategies/base.py
"""Base strategy interface for Kalshi auto-trader."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.kalshi_client import KalshiClient


@dataclass
class OrderParams:
    """Parameters for placing an order."""
    ticker: str
    side: str              # 'yes' or 'no'
    action: str            # 'buy' or 'sell'
    price_cents: int       # Limit price in cents (1-99)
    quantity: int          # Number of contracts
    post_only: bool        # True to avoid taker fees
    market_snapshot: Dict[str, Any]  # Full market data for DB

    def __post_init__(self):
        if not 1 <= self.price_cents <= 99:
            raise ValueError(
                f"price_cents must be 1-99, got {self.price_cents} "
                f"(ticker={self.ticker})"
            )
        if self.quantity <= 0:
            raise ValueError(
                f"quantity must be > 0, got {self.quantity} "
                f"(ticker={self.ticker})"
            )
        if self.side not in ("yes", "no"):
            raise ValueError(
                f"side must be 'yes' or 'no', got '{self.side}' "
                f"(ticker={self.ticker})"
            )


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    def __init__(self, client: "KalshiClient", config: Dict[str, Any]):
        self.client = client
        self.config = config

    @property
    def name(self) -> str:
        """Strategy name from config."""
        return self.config.get("name", self.__class__.__name__.lower())

    @abstractmethod
    def find_markets(self) -> List[Dict[str, Any]]:
        """Find markets matching this strategy's filters."""
        pass

    @abstractmethod
    def calculate_order(self, market: Dict[str, Any]) -> Optional[OrderParams]:
        """Calculate order parameters for a market. Return None to skip."""
        pass

    def calculate_orders(self, market: Dict[str, Any]) -> List[OrderParams]:
        """Calculate one or more orders for a market.

        Default: delegates to calculate_order() for single-order strategies.
        Override for multi-order strategies (e.g., ladder).
        """
        single = self.calculate_order(market)
        return [single] if single else []

    def validate_config(self) -> None:
        """Validate strategy-specific configuration. Override in subclasses."""
        pass
