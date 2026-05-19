# tests/test_main.py
import pytest
from unittest.mock import MagicMock, patch
import argparse

from src.main import parse_args, run_strategy


class TestParseArgs:
    def test_strategy_arg(self):
        args = parse_args(["--strategy", "longshot"])
        assert args.strategy == "longshot"
        assert args.check_settlements is False

    def test_check_settlements_arg(self):
        args = parse_args(["--check-settlements"])
        assert args.check_settlements is True
        assert args.strategy is None

    def test_list_strategies_arg(self):
        args = parse_args(["--list-strategies"])
        assert args.list_strategies is True

    def test_performance_arg(self):
        args = parse_args(["--performance"])
        assert args.performance == "all"

    def test_performance_with_strategy(self):
        args = parse_args(["--performance", "mentions"])
        assert args.performance == "mentions"

    def test_dry_run_override(self):
        args = parse_args(["--strategy", "longshot", "--dry-run"])
        assert args.dry_run is True

    def test_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            parse_args(["--strategy", "longshot", "--check-settlements"])

    def test_confirm_arg(self):
        args = parse_args(["--strategy", "longshot", "--confirm"])
        assert args.confirm is True

    def test_confirm_default_false(self):
        args = parse_args(["--strategy", "longshot"])
        assert args.confirm is False

    def test_guard_arg(self):
        args = parse_args(["--guard"])
        assert args.guard is True
        assert args.strategy is None

    def test_guard_with_options(self):
        args = parse_args([
            "--guard", "--once",
            "--cancel-buffer", "10",
            "--poll-interval", "3",
            "--min-confidence", "low",
            "--dry-run",
        ])
        assert args.guard is True
        assert args.once is True
        assert args.cancel_buffer == 10
        assert args.poll_interval == 3
        assert args.min_confidence == "low"
        assert args.dry_run is True

    def test_guard_defaults(self):
        args = parse_args(["--guard"])
        assert args.cancel_buffer == 60
        assert args.poll_interval == 2
        assert args.min_confidence == "medium"
        assert args.once is False

    def test_guard_mutually_exclusive_with_strategy(self):
        with pytest.raises(SystemExit):
            parse_args(["--guard", "--strategy", "longshot"])

    def test_run_arg(self):
        args = parse_args(["--run"])
        assert args.run is True
        assert args.strategy is None
        assert args.guard is False

    def test_run_with_dry_run(self):
        args = parse_args(["--run", "--dry-run"])
        assert args.run is True
        assert args.dry_run is True

    def test_run_mutually_exclusive_with_strategy(self):
        with pytest.raises(SystemExit):
            parse_args(["--run", "--strategy", "longshot"])

    def test_run_mutually_exclusive_with_guard(self):
        with pytest.raises(SystemExit):
            parse_args(["--run", "--guard"])

    def test_run_with_mentions_flag(self):
        args = parse_args(["--run", "--mentions"])
        assert args.run is True
        assert args.mentions is True
        assert args.longshot is False

    def test_run_with_longshot_flag(self):
        args = parse_args(["--run", "--longshot"])
        assert args.run is True
        assert args.longshot is True
        assert args.mentions is False

    def test_run_with_both_strategy_flags(self):
        args = parse_args(["--run", "--mentions", "--longshot"])
        assert args.run is True
        assert args.mentions is True
        assert args.longshot is True

    def test_strategy_flags_default_false(self):
        args = parse_args(["--run"])
        assert args.mentions is False
        assert args.longshot is False


class TestRunStrategyGuard:
    """One-shot mode requires XAI_API_KEY and checks event start times."""

    @patch("src.main.load_config")
    def test_errors_without_grok_api_key(self, mock_load_config):
        """run_strategy returns 1 when XAI_API_KEY is not set."""
        mock_config = MagicMock()
        mock_config.grok_api_key = None
        mock_config.dry_run = False
        mock_config.log_level = "WARNING"
        mock_load_config.return_value = mock_config

        result = run_strategy("mentions", None, dry_run=True)
        assert result == 1
