# tests/test_strategies.py
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from src.strategies import discover_guarded_strategies, discover_enabled_strategies, discover_strategies


class TestDiscoverGuardedStrategies:
    def test_returns_mentions_when_guard_true(self):
        """Mentions strategy has guard: true in its YAML config."""
        result = discover_guarded_strategies()
        assert "mentions" in result
        assert "longshot" not in result

    def test_returns_list(self):
        """Return type is a list."""
        result = discover_guarded_strategies()
        assert isinstance(result, list)


class TestDiscoverEnabledStrategies:
    def test_returns_only_enabled_strategies(self):
        """Only strategies with enabled: true in YAML are returned."""
        result = discover_enabled_strategies()
        assert "mentions" in result
        assert "longshot" not in result

    def test_returns_list(self):
        """Return type is a list."""
        result = discover_enabled_strategies()
        assert isinstance(result, list)


class TestDiscoverStrategies:
    def test_finds_known_strategies(self):
        """Should find both mentions and longshot strategies."""
        result = discover_strategies()
        assert "mentions" in result
        assert "longshot" in result
