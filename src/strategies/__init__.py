# src/strategies/__init__.py
"""Strategy plugin system for Kalshi auto-trader."""
import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Type

import yaml

logger = logging.getLogger(__name__)

from src.strategies.base import BaseStrategy, OrderParams

__all__ = ["BaseStrategy", "OrderParams", "discover_strategies", "discover_enabled_strategies", "discover_guarded_strategies", "load_strategy_config", "get_strategy_class"]


def discover_strategies() -> List[str]:
    """Discover available strategy modules.

    Returns:
        List of strategy names (module names without .py)
    """
    strategies_dir = Path(__file__).parent
    strategies = []

    for item in strategies_dir.iterdir():
        if item.suffix == ".py" and item.stem not in ("__init__", "base"):
            strategies.append(item.stem)

    return strategies


def discover_enabled_strategies() -> List[str]:
    """Discover strategies that have enabled: true in their YAML config.

    Returns:
        List of strategy names with enabled set to true
    """
    strategies_dir = Path(__file__).parent.parent.parent / "strategies"
    enabled = []

    for item in strategies_dir.iterdir():
        if item.suffix == ".yaml":
            try:
                config = load_strategy_config(str(item))
                name = config.get("name", item.stem)
                is_enabled = config.get("enabled", False)
                logger.debug(f"Strategy YAML {item.name}: enabled={is_enabled}")
                if is_enabled:
                    enabled.append(name)
            except Exception as e:
                logger.warning(f"Error reading strategy config {item}: {e}", exc_info=True)
                continue

    logger.info(f"Enabled strategies: {enabled}")
    return enabled


def discover_guarded_strategies() -> List[str]:
    """Discover strategies that have guard: true in their YAML config.

    Returns:
        List of strategy names with guard enabled
    """
    strategies_dir = Path(__file__).parent.parent.parent / "strategies"
    guarded = []

    for item in strategies_dir.iterdir():
        if item.suffix == ".yaml":
            try:
                config = load_strategy_config(str(item))
                if config.get("guard", False):
                    guarded.append(config.get("name", item.stem))
            except Exception as e:
                logger.warning(f"Error reading strategy config {item}: {e}", exc_info=True)
                continue

    logger.debug(f"Guarded strategies: {guarded}")
    return guarded


def load_strategy_config(config_path: str) -> Dict[str, Any]:
    """Load strategy configuration from YAML file.

    Args:
        config_path: Path to YAML config file

    Returns:
        Parsed configuration dict

    Raises:
        FileNotFoundError: If config file doesn't exist
        yaml.YAMLError: If config is invalid YAML
    """
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_strategy_class(strategy_name: str) -> Type[BaseStrategy]:
    """Get strategy class by name.

    Args:
        strategy_name: Name of the strategy (e.g., 'longshot', 'mentions')

    Returns:
        Strategy class (subclass of BaseStrategy)

    Raises:
        ImportError: If strategy module not found
        AttributeError: If module doesn't export expected class
    """
    module = importlib.import_module(f"src.strategies.{strategy_name}")

    # Convention: class name is CamelCase of strategy name + "Strategy"
    class_name = f"{strategy_name.title()}Strategy"

    return getattr(module, class_name)
