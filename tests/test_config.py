import os
import pytest
from unittest.mock import patch

def test_load_config_success():
    """Config loads all required values from environment."""
    env = {
        "KALSHI_API_KEY_ID": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": "/tmp/test.pem",
        "KALSHI_API_BASE_URL": "https://api.test.com",
        "CONTRACT_COUNT": "10",
        "MIN_YES_PRICE": "0.02",
        "MAX_YES_PRICE": "0.19",
        "SPORTS_CATEGORIES": "NFL,NBA",
        "DRY_RUN": "false",
        "LOG_LEVEL": "INFO",
    }
    with patch.dict(os.environ, env, clear=True):
        from src.config import load_config
        config = load_config(skip_file_check=True)

        assert config.api_key_id == "test-key-id"
        assert config.private_key_path == "/tmp/test.pem"
        assert config.contract_count == 10
        assert config.min_yes_price == 0.02
        assert config.max_yes_price == 0.19
        # NO prices are derived from YES prices
        assert config.min_no_price == 0.81  # 1 - 0.19
        assert config.max_no_price == 0.98  # 1 - 0.02
        assert config.sports_categories == ["NFL", "NBA"]
        assert config.dry_run is False


def test_load_config_missing_api_key():
    """Config raises error when API key missing."""
    env = {
        "KALSHI_PRIVATE_KEY_PATH": "/tmp/test.pem",
    }
    with patch.dict(os.environ, env, clear=True):
        from src.config import load_config, ConfigError
        with pytest.raises(ConfigError, match="KALSHI_API_KEY_ID is required"):
            load_config(skip_file_check=True)


def test_load_config_invalid_price_range():
    """Config raises error when min >= max price."""
    env = {
        "KALSHI_API_KEY_ID": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": "/tmp/test.pem",
        "MIN_YES_PRICE": "0.50",
        "MAX_YES_PRICE": "0.20",
    }
    with patch.dict(os.environ, env, clear=True):
        from src.config import load_config, ConfigError
        with pytest.raises(ConfigError, match="MIN_YES_PRICE must be less than"):
            load_config(skip_file_check=True)


def test_load_config_invalid_subaccount():
    """Config raises error when subaccount out of range."""
    env = {
        "KALSHI_API_KEY_ID": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": "/tmp/test.pem",
        "SUBACCOUNT_NUMBER": "50",
    }
    with patch.dict(os.environ, env, clear=True):
        from src.config import load_config, ConfigError
        with pytest.raises(ConfigError, match="SUBACCOUNT_NUMBER must be between 1 and 32"):
            load_config(skip_file_check=True)


def test_load_config_optional_subaccount():
    """Config allows empty subaccount (uses primary account)."""
    env = {
        "KALSHI_API_KEY_ID": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": "/tmp/test.pem",
        "SUBACCOUNT_NUMBER": "",
    }
    with patch.dict(os.environ, env, clear=True):
        from src.config import load_config
        config = load_config(skip_file_check=True)
        assert config.subaccount_number is None


def test_load_config_with_database_path(monkeypatch, tmp_path):
    """Config loads custom database path from environment."""
    key_file = tmp_path / "key.pem"
    key_file.write_text("fake key")

    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setenv("DATABASE_PATH", "custom/path/orders.db")

    from src.config import load_config
    config = load_config()

    assert config.database_path == "custom/path/orders.db"


def test_load_config_default_database_path(monkeypatch, tmp_path):
    """Config uses default database path when not specified."""
    key_file = tmp_path / "key.pem"
    key_file.write_text("fake key")

    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key_file))
    # Don't set DATABASE_PATH

    from src.config import load_config
    config = load_config()

    assert config.database_path == "data/orders.db"
