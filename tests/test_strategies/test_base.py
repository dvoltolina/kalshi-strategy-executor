# tests/test_strategies/test_base.py
import os
import tempfile

import pytest
import yaml
from unittest.mock import MagicMock

from src.strategies.base import BaseStrategy, OrderParams
from src.strategies import load_strategy_config, discover_strategies, get_strategy_class


class TestOrderParams:
    def test_order_params_dataclass(self):
        params = OrderParams(
            ticker="TEST-MARKET",
            side="no",
            action="buy",
            price_cents=85,
            quantity=10,
            post_only=True,
            market_snapshot={"yes_bid": 15},
        )

        assert params.ticker == "TEST-MARKET"
        assert params.side == "no"
        assert params.price_cents == 85
        assert params.post_only is True


    def test_rejects_price_below_1(self):
        with pytest.raises(ValueError, match="price_cents must be 1-99"):
            OrderParams(
                ticker="MKT", side="no", action="buy",
                price_cents=0, quantity=10, post_only=True,
                market_snapshot={},
            )

    def test_rejects_price_above_99(self):
        with pytest.raises(ValueError, match="price_cents must be 1-99"):
            OrderParams(
                ticker="MKT", side="no", action="buy",
                price_cents=100, quantity=10, post_only=True,
                market_snapshot={},
            )

    def test_rejects_zero_quantity(self):
        with pytest.raises(ValueError, match="quantity must be > 0"):
            OrderParams(
                ticker="MKT", side="no", action="buy",
                price_cents=50, quantity=0, post_only=True,
                market_snapshot={},
            )

    def test_rejects_invalid_side(self):
        with pytest.raises(ValueError, match="side must be"):
            OrderParams(
                ticker="MKT", side="maybe", action="buy",
                price_cents=50, quantity=10, post_only=True,
                market_snapshot={},
            )

    def test_accepts_boundary_prices(self):
        """price_cents=1 and price_cents=99 should both be valid."""
        p1 = OrderParams(
            ticker="MKT", side="no", action="buy",
            price_cents=1, quantity=1, post_only=True,
            market_snapshot={},
        )
        assert p1.price_cents == 1

        p99 = OrderParams(
            ticker="MKT", side="yes", action="buy",
            price_cents=99, quantity=1, post_only=True,
            market_snapshot={},
        )
        assert p99.price_cents == 99


class TestBaseStrategy:
    def test_cannot_instantiate_base_strategy(self):
        mock_client = MagicMock()
        config = {"name": "test"}

        with pytest.raises(TypeError):
            BaseStrategy(mock_client, config)

    def test_concrete_strategy_must_implement_abstract_methods(self):
        mock_client = MagicMock()
        config = {"name": "test"}

        class IncompleteStrategy(BaseStrategy):
            pass

        with pytest.raises(TypeError):
            IncompleteStrategy(mock_client, config)

    def test_concrete_strategy_works(self):
        mock_client = MagicMock()
        config = {"name": "test", "filters": {}}

        class ConcreteStrategy(BaseStrategy):
            def find_markets(self):
                return []

            def calculate_order(self, market):
                return None

        strategy = ConcreteStrategy(mock_client, config)
        assert strategy.client == mock_client
        assert strategy.config == config


class TestStrategyDiscovery:
    def test_discover_strategies_finds_modules(self):
        """Should find strategy modules in src/strategies/."""
        strategies = discover_strategies()
        assert isinstance(strategies, list)

    def test_load_strategy_config(self):
        """Should load and parse YAML config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "test.yaml")
            config_data = {
                "name": "test_strategy",
                "description": "A test strategy",
                "filters": {
                    "min_yes_price": 0.02,
                    "max_yes_price": 0.19,
                },
                "order": {
                    "side": "no",
                    "contracts_per_market": 10,
                },
            }

            with open(config_path, "w") as f:
                yaml.dump(config_data, f)

            loaded = load_strategy_config(config_path)

            assert loaded["name"] == "test_strategy"
            assert loaded["filters"]["min_yes_price"] == 0.02
            assert loaded["order"]["contracts_per_market"] == 10
