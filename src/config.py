import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Config:
    api_key_id: str
    private_key_path: str
    api_base_url: str
    subaccount_number: Optional[int]
    contract_count: int
    min_yes_price: float
    max_yes_price: float
    min_no_price: float  # Derived: 1 - max_yes_price
    max_no_price: float  # Derived: 1 - min_yes_price
    sports_categories: List[str]
    dry_run: bool
    log_level: str
    database_path: str
    grok_api_key: Optional[str]
    max_total_notional_usd: Optional[float]  # None = unlimited (dry-run only)


class ConfigError(Exception):
    """Raised when configuration is invalid."""
    pass


def load_config(skip_file_check: bool = False) -> Config:
    """Load configuration from environment variables.

    Args:
        skip_file_check: If True, skip checking if private key file exists (for testing)

    Returns:
        Config object with all settings

    Raises:
        ConfigError: If required config is missing or invalid
    """
    api_key_id = os.environ.get("KALSHI_API_KEY_ID")
    if not api_key_id:
        raise ConfigError("KALSHI_API_KEY_ID is required")

    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not private_key_path:
        raise ConfigError("KALSHI_PRIVATE_KEY_PATH is required")

    if not skip_file_check and not os.path.exists(private_key_path):
        raise ConfigError(f"Private key file not found: {private_key_path}")

    api_base_url = os.environ.get(
        "KALSHI_API_BASE_URL",
        "https://api.elections.kalshi.com/trade-api/v2"
    )

    subaccount_str = os.environ.get("SUBACCOUNT_NUMBER", "").strip()
    subaccount_number = None
    if subaccount_str:
        try:
            subaccount_number = int(subaccount_str)
            if not 1 <= subaccount_number <= 32:
                raise ConfigError("SUBACCOUNT_NUMBER must be between 1 and 32")
        except ValueError:
            raise ConfigError("SUBACCOUNT_NUMBER must be an integer")

    try:
        contract_count = int(os.environ.get("CONTRACT_COUNT", "10"))
        if contract_count < 1:
            raise ConfigError("CONTRACT_COUNT must be at least 1")
    except ValueError:
        raise ConfigError("CONTRACT_COUNT must be an integer")

    try:
        min_yes_price = float(os.environ.get("MIN_YES_PRICE", "0.02"))
        max_yes_price = float(os.environ.get("MAX_YES_PRICE", "0.19"))
    except ValueError:
        raise ConfigError("MIN_YES_PRICE and MAX_YES_PRICE must be numbers")

    if not (0.01 <= min_yes_price <= 0.99):
        raise ConfigError("MIN_YES_PRICE must be between 0.01 and 0.99")
    if not (0.01 <= max_yes_price <= 0.99):
        raise ConfigError("MAX_YES_PRICE must be between 0.01 and 0.99")
    if min_yes_price >= max_yes_price:
        raise ConfigError("MIN_YES_PRICE must be less than MAX_YES_PRICE")

    categories_str = os.environ.get("SPORTS_CATEGORIES", "Sports")
    sports_categories = [c.strip() for c in categories_str.split(",") if c.strip()]
    if not sports_categories:
        raise ConfigError("SPORTS_CATEGORIES must have at least one category")

    dry_run_str = os.environ.get("DRY_RUN", "false").lower()
    dry_run = dry_run_str in ("true", "1", "yes")

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")

    database_path = os.environ.get("DATABASE_PATH", "data/orders.db")

    grok_api_key = os.environ.get("XAI_API_KEY")

    mtn_str = os.environ.get("MAX_TOTAL_NOTIONAL_USD", "").strip()
    max_total_notional_usd: Optional[float]
    if mtn_str:
        try:
            max_total_notional_usd = float(mtn_str)
        except ValueError:
            raise ConfigError("MAX_TOTAL_NOTIONAL_USD must be a number")
        if max_total_notional_usd <= 0:
            raise ConfigError("MAX_TOTAL_NOTIONAL_USD must be > 0")
    else:
        max_total_notional_usd = None

    # Hard requirement when not in dry-run: live trading must have a $ cap.
    if not dry_run and max_total_notional_usd is None:
        raise ConfigError(
            "MAX_TOTAL_NOTIONAL_USD must be set when DRY_RUN=false "
            "(safety cap for live trading)"
        )

    # Derive NO price range from YES price range (complement)
    min_no_price = 1.0 - max_yes_price  # e.g., 1 - 0.19 = 0.81
    max_no_price = 1.0 - min_yes_price  # e.g., 1 - 0.02 = 0.98

    return Config(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        api_base_url=api_base_url,
        subaccount_number=subaccount_number,
        contract_count=contract_count,
        min_yes_price=min_yes_price,
        max_yes_price=max_yes_price,
        min_no_price=min_no_price,
        max_no_price=max_no_price,
        sports_categories=sports_categories,
        dry_run=dry_run,
        log_level=log_level,
        database_path=database_path,
        grok_api_key=grok_api_key,
        max_total_notional_usd=max_total_notional_usd,
    )
