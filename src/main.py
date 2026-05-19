# src/main.py
"""Kalshi Auto-Trader - CLI entry point."""
import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

from src.config import load_config, ConfigError
from src.database import Database
from src.kalshi_client import KalshiClient, KalshiAPIError
from src.logging_config import setup_logging
from src.order_guard import OrderGuard
from src.order_placer import OrderPlacer, build_market_url
from src.settlement_checker import SettlementChecker
from src.strategies import discover_strategies, discover_enabled_strategies, discover_guarded_strategies, get_strategy_class, load_strategy_config


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Kalshi Auto-Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python -m src.main --strategy longshot
  uv run python -m src.main --strategy mentions --dry-run
  uv run python -m src.main --check-settlements
  uv run python -m src.main --performance
  uv run python -m src.main --list-strategies
  uv run python -m src.main --guard --dry-run
  uv run python -m src.main --guard --once --dry-run
  uv run python -m src.main --run
  uv run python -m src.main --run --dry-run
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--strategy", "-s",
        help="Strategy to run (e.g., longshot, mentions)",
    )
    group.add_argument(
        "--check-settlements",
        action="store_true",
        help="Check and record settlement outcomes",
    )
    group.add_argument(
        "--list-strategies",
        action="store_true",
        help="List available strategies",
    )
    group.add_argument(
        "--performance",
        nargs="?",
        const="all",
        help="Show performance summary (optionally filter by strategy)",
    )
    group.add_argument(
        "--guard",
        action="store_true",
        help="Monitor and auto-cancel resting orders before events start",
    )
    group.add_argument(
        "--run",
        action="store_true",
        help="Run continuously: trade at :00/:30, guard every 5min, settle automatically",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override DRY_RUN=true regardless of .env",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Prompt for confirmation before each order",
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to strategy YAML (default: strategies/{name}.yaml)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=2,
        choices=range(1, 61),
        metavar="MINUTES",
        help="Guard polling interval in minutes, 1-60 (default: 2)",
    )
    parser.add_argument(
        "--cancel-buffer",
        type=int,
        default=60,
        help="Cancel orders this many minutes before event start (default: 60)",
    )
    parser.add_argument(
        "--min-confidence",
        choices=["high", "medium", "low"],
        default="medium",
        help="Minimum confidence level for start time estimates (default: medium)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run guard once then exit (no polling)",
    )

    # Strategy override flags for --run mode
    parser.add_argument(
        "--mentions",
        action="store_true",
        help="Enable mentions strategy (overrides YAML enabled setting)",
    )
    parser.add_argument(
        "--longshot",
        action="store_true",
        help="Enable longshot strategy (overrides YAML enabled setting)",
    )

    return parser.parse_args(args)


def run_strategy(strategy_name: str, config_path: Optional[str], dry_run: bool, confirm: bool = False) -> int:
    """Run a trading strategy.

    Returns:
        Exit code (0=success, 1=error)
    """
    # Load global config
    try:
        global_config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 1

    # Setup logging
    log_file = setup_logging(global_config.log_level)

    # Override dry_run if specified
    if dry_run:
        global_config = global_config.__class__(
            **{**global_config.__dict__, "dry_run": True}
        )

    # Print header
    print()
    print(f"Kalshi Auto-Trader - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Strategy: {strategy_name}")
    print("=" * 50)

    # Require XAI_API_KEY for event start time safety checks
    if not global_config.grok_api_key:
        print("Error: XAI_API_KEY is required for event start time safety checks")
        print("Set XAI_API_KEY in .env to enable trading")
        return 1

    logger.info(f"Starting one-shot run: strategy={strategy_name}, dry_run={global_config.dry_run}, config={config_path}")

    if global_config.dry_run:
        print("[DRY RUN MODE - No orders will be placed]")
        print()

    # Load strategy config
    if config_path is None:
        config_path = f"strategies/{strategy_name}.yaml"

    try:
        strategy_config = load_strategy_config(config_path)
        logger.debug(f"Strategy config loaded from {config_path}: {strategy_config}")
    except FileNotFoundError:
        logger.error(f"Strategy config not found: {config_path}", exc_info=True)
        print(f"Strategy config not found: {config_path}")
        return 1

    # Load private key
    try:
        with open(global_config.private_key_path, "r") as f:
            private_key_pem = f.read()
    except Exception as e:
        print(f"Error reading private key: {e}")
        return 1

    # Initialize client
    client = KalshiClient(
        api_key_id=global_config.api_key_id,
        private_key_pem=private_key_pem,
        base_url=global_config.api_base_url,
    )

    # Check exchange status
    try:
        status = client.get_exchange_status()
        logger.debug(f"Exchange status: {status}")
        if not status.get("trading_active"):
            logger.warning("Exchange not active for trading")
            print("Exchange is not active for trading. Try again later.")
            return 1
    except KalshiAPIError as e:
        logger.error(f"Failed to check exchange status: {e.message}", exc_info=True)
        print(f"Failed to check exchange status: {e.message}")
        return 1

    # Get strategy class and instantiate
    try:
        StrategyClass = get_strategy_class(strategy_name)
    except (ImportError, AttributeError) as e:
        print(f"Failed to load strategy '{strategy_name}': {e}")
        return 1

    strategy = StrategyClass(client, strategy_config)

    # Initialize database for deduplication check (always needed)
    db = Database(global_config.database_path)

    # Find markets
    print("Scanning for qualifying markets...")
    try:
        markets = strategy.find_markets()
        logger.info(f"Market scan complete: {len(markets)} qualifying markets found")
    except KalshiAPIError as e:
        logger.error(f"Error scanning markets for {strategy_name}: {e.message}", exc_info=True)
        print(f"Error scanning markets: {e.message}")
        return 1

    # Filter out already-traded markets (default: enabled)
    skip_already_traded = strategy_config.get("skip_already_traded", True)
    if skip_already_traded and markets:
        traded_tickers = db.get_traded_tickers(strategy=strategy_name, pending_only=True)
        if traded_tickers:
            logger.debug(f"Already-traded tickers: {traded_tickers}")
            original_count = len(markets)
            markets = [m for m in markets if m["ticker"] not in traded_tickers]
            skipped = original_count - len(markets)
            if skipped > 0:
                logger.info(f"Skipped {skipped} already-traded markets")
                print(f"  Skipped {skipped} already-traded markets")

    if not markets:
        logger.info("No qualifying markets after filtering")
        print()
        print("No qualifying markets found.")
        print(f"Log saved to: {log_file}")
        return 0

    # Initialize guard for event start time safety checks
    guard = OrderGuard(
        client=client,
        db=db,
        grok_api_key=global_config.grok_api_key,
        cancel_buffer_minutes=60,
        min_confidence="medium",
        dry_run=global_config.dry_run,
    )

    # Warm cache for all discovered markets
    market_tickers = [m["ticker"] for m in markets]
    print(f"Checking event start times for {len(markets)} markets...")
    guard.warm_cache(market_tickers)

    # Place orders
    print()
    print(f"Placing orders on {len(markets)} markets:")
    print()

    placer = OrderPlacer(
        client=client,
        dry_run=global_config.dry_run,
    )

    successes = 0
    failures = 0

    for market in markets:
        # Guard check: skip markets whose events have started or are imminent
        event_ticker = market.get("event_ticker", "")
        if event_ticker:
            cached = guard.event_cache.get(event_ticker)
            if not cached:
                # Not in cache — query milestones + Grok on demand
                fallback = guard._query_milestones(
                    [event_ticker], {event_ticker: [{"ticker": market["ticker"]}]}
                )
                if fallback:
                    guard._query_start_times(
                        fallback, {event_ticker: [{"ticker": market["ticker"]}]}
                    )
                cached = guard.event_cache.get(event_ticker)
            if not cached:
                logger.warning(f"No start time for event {event_ticker}, blocking {market['ticker']}")
                print(f"  [skip] {market['ticker']}: unknown event start time, blocking as precaution")
                continue
            from datetime import timezone as tz
            now_utc = datetime.now(tz.utc)
            mins_until = (cached["estimated_start_utc"] - now_utc).total_seconds() / 60
            if mins_until <= guard.cancel_buffer_minutes:
                logger.info(f"Event {event_ticker} starts in {mins_until:.0f}min, skipping {market['ticker']}")
                print(f"  [skip] {market['ticker']}: event starts in {mins_until:.0f}min (<= {guard.cancel_buffer_minutes}min buffer)")
                continue

        order_list = strategy.calculate_orders(market)
        if not order_list:
            continue

        for order_params in order_list:
            # Build market URL for display
            snapshot = order_params.market_snapshot or {}
            event_ticker = snapshot.get("event_ticker", "")
            title = snapshot.get("title", "")
            market_url = build_market_url(order_params.ticker, event_ticker, title)

            # Confirmation mode - show details and prompt
            if confirm:
                yes_bid = snapshot.get("_yes_bid") or snapshot.get("yes_bid")
                if yes_bid and yes_bid > 1:
                    yes_bid = yes_bid / 100

                print("-" * 60)
                print(f"Market: {title or order_params.ticker}")
                print(f"Ticker: {order_params.ticker}")
                print(f"URL: {market_url}")
                print()
                print(f"  Order: BUY {order_params.quantity} {order_params.side.upper()} @ {order_params.price_cents}c")
                if yes_bid:
                    print(f"  YES bid: ${yes_bid:.2f}")
                print(f"  Total cost: ${order_params.price_cents * order_params.quantity / 100:.2f}")
                print()

                response = input("Place this order? [y/N]: ").strip().lower()
                if response != "y":
                    print("  [skip] Order skipped")
                    continue

            result = placer.place_order_from_params(order_params, strategy=strategy_name)

            if result.success:
                status_icon = "[ok]" if not result.dry_run else "[dry]"
                print(
                    f"  {status_icon} {order_params.ticker} @ {order_params.price_cents}c "
                    f"{order_params.side.upper()}"
                )
                successes += 1

                # Record to database
                if db and not result.dry_run:
                    db.record_order(
                        client_order_id=result.client_order_id,
                        order_id=result.order_id,
                        strategy=strategy_name,
                        ticker=order_params.ticker,
                        side=order_params.side,
                        action=order_params.action,
                        price_cents=order_params.price_cents,
                        quantity=order_params.quantity,
                        status="pending",
                        market_snapshot=order_params.market_snapshot,
                    )
            else:
                print(f"  [fail] {order_params.ticker}: {result.error}")
                failures += 1

    # Cleanup
    if db:
        db.close()

    # Print summary
    print()
    print(f"Summary: {successes} orders placed, {failures} failed")
    print(f"Log saved to: {log_file}")

    logger.info(f"One-shot run complete: strategy={strategy_name}, placed={successes}, failed={failures}")

    return 0 if failures == 0 else 1


def run_check_settlements() -> int:
    """Check settlement outcomes for all orders."""
    try:
        global_config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 1

    setup_logging(global_config.log_level)

    print()
    print(f"Kalshi Auto-Trader - Settlement Check")
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # Load private key
    try:
        with open(global_config.private_key_path, "r") as f:
            private_key_pem = f.read()
    except Exception as e:
        print(f"Error reading private key: {e}")
        return 1

    client = KalshiClient(
        api_key_id=global_config.api_key_id,
        private_key_pem=private_key_pem,
        base_url=global_config.api_base_url,
    )

    db = Database(global_config.database_path)
    checker = SettlementChecker(client, db)

    print("Checking unsettled orders...")
    results = checker.check_all()

    print()
    print(f"Updated: {results['updated']}")
    print(f"Still pending: {results['still_pending']}")
    print(f"Errors: {results['errors']}")

    db.close()
    return 0


def run_list_strategies() -> int:
    """List available strategies."""
    print()
    print("Available strategies:")
    print()

    strategies = discover_strategies()
    if not strategies:
        print("  (none found)")
        return 0

    for name in sorted(strategies):
        config_path = f"strategies/{name}.yaml"
        try:
            config = load_strategy_config(config_path)
            description = config.get("description", "No description")
        except FileNotFoundError:
            description = "(no config file)"

        print(f"  {name}: {description}")

    return 0


def run_performance(strategy_filter: str) -> int:
    """Show performance summary with ROI and per-market breakdown."""
    try:
        global_config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 1

    db = Database(global_config.database_path)

    strategy = None if strategy_filter == "all" else strategy_filter

    print()
    if strategy:
        print(f"Performance Summary - {strategy}")
    else:
        print("Performance Summary - All Strategies")
    print("=" * 60)

    # Get per-order detail
    orders = db.get_settled_orders_detail(strategy=strategy)

    if not orders:
        summary = db.get_performance_summary(strategy=strategy)
        print(f"Total orders:  {summary['total_orders']}")
        print(f"Settled:       0")
        print()
        print("No settled orders yet.")
        db.close()
        return 0

    # Separate filled trades from zero-fill orders
    filled_orders = []
    zero_fill_count = 0
    total_cost_cents = 0
    total_pnl_cents = 0
    wins = []
    losses = []

    for o in orders:
        qty = o["filled_quantity"] if o["filled_quantity"] is not None else o["quantity"]
        pnl = o["pnl_cents"] or 0

        if qty == 0:
            zero_fill_count += 1
            continue

        filled_orders.append(o)
        cost = o["price_cents"] * qty
        total_cost_cents += cost
        total_pnl_cents += pnl
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)

    summary = db.get_performance_summary(strategy=strategy)
    traded = len(filled_orders)
    win_count = len(wins)
    loss_count = len(losses)

    # Summary stats
    print(f"Total orders:    {summary['total_orders']}")
    print(f"Settled:         {len(orders)}  ({zero_fill_count} unfilled, {traded} traded)")
    print(f"Wins:            {win_count}")
    print(f"Losses:          {loss_count}")
    if traded > 0:
        print(f"Win rate:        {win_count / traded * 100:.1f}%")
    print()

    # Financial summary
    total_pnl = total_pnl_cents / 100
    total_cost = total_cost_cents / 100
    roi = (total_pnl_cents / total_cost_cents * 100) if total_cost_cents > 0 else 0

    print(f"Cost basis:      ${total_cost:,.2f}")
    print(f"Total P&L:       ${total_pnl:+,.2f}")
    print(f"ROI:             {roi:+.1f}%")
    print()

    if wins:
        avg_win = sum(wins) / len(wins) / 100
        best_win = max(wins) / 100
        print(f"Avg win:         ${avg_win:+,.2f}")
        print(f"Best win:        ${best_win:+,.2f}")
    if losses:
        avg_loss = sum(losses) / len(losses) / 100
        worst_loss = min(losses) / 100
        print(f"Avg loss:        ${avg_loss:+,.2f}")
        print(f"Worst loss:      ${worst_loss:+,.2f}")

    # Per-market breakdown (only filled trades)
    print()
    print("Settled Orders (filled only)")
    print("-" * 60)

    # Column headers
    print(f"{'Market':<30} {'Side':>4} {'Qty':>5} {'Entry':>6} {'P&L':>9} {'ROI':>7}")
    print(f"{'':<30} {'':>4} {'':>5} {'':>6} {'':>9} {'':>7}")

    for o in filled_orders:
        qty = o["filled_quantity"] if o["filled_quantity"] is not None else o["quantity"]
        pnl = (o["pnl_cents"] or 0) / 100
        cost = o["price_cents"] * qty
        order_roi = ((o["pnl_cents"] or 0) / cost * 100) if cost > 0 else 0
        title = o["title"] or o["ticker"]
        # Truncate long titles
        if len(title) > 28:
            title = title[:26] + ".."

        print(
            f"{title:<30} {o['side'].upper():>4} {qty:>5} "
            f"{o['price_cents']:>5}c ${pnl:>+8,.2f} {order_roi:>+6.1f}%"
        )

    db.close()
    return 0


def run_continuous(args: argparse.Namespace) -> int:
    """Run the continuous trading loop."""
    try:
        global_config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 1

    if args.dry_run:
        global_config = global_config.__class__(
            **{**global_config.__dict__, "dry_run": True}
        )

    log_file = setup_logging(global_config.log_level)

    # Build strategy list: start with YAML-enabled, union in CLI overrides
    enabled_from_yaml = discover_enabled_strategies()
    strategies = set(enabled_from_yaml)
    cli_overrides = []
    if args.mentions:
        strategies.add("mentions")
        if "mentions" not in enabled_from_yaml:
            cli_overrides.append("mentions")
    if args.longshot:
        strategies.add("longshot")
        if "longshot" not in enabled_from_yaml:
            cli_overrides.append("longshot")
    strategies = sorted(strategies) if strategies else None

    logger.info(
        f"Strategy resolution: yaml_enabled={enabled_from_yaml}, "
        f"cli_overrides={cli_overrides}, final={strategies}"
    )

    # Load private key
    try:
        with open(global_config.private_key_path, "r") as f:
            private_key_pem = f.read()
    except Exception as e:
        print(f"Error reading private key: {e}")
        return 1

    client = KalshiClient(
        api_key_id=global_config.api_key_id,
        private_key_pem=private_key_pem,
        base_url=global_config.api_base_url,
    )

    db = Database(global_config.database_path)

    from src.runner import Runner

    runner = Runner(
        config=global_config,
        client=client,
        db=db,
        dry_run=global_config.dry_run,
        strategies=strategies,
    )

    runner.start()  # Blocks until Ctrl+C
    return 0


def run_guard(args: argparse.Namespace) -> int:
    """Run the order guard to auto-cancel orders before events start."""
    try:
        global_config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 1

    if not global_config.grok_api_key:
        print("Error: XAI_API_KEY environment variable is required for --guard")
        return 1

    log_file = setup_logging(global_config.log_level)

    dry_run = args.dry_run or global_config.dry_run

    guarded = discover_guarded_strategies()
    guard_label = ", ".join(guarded) if guarded else "all"

    print()
    print(f"Kalshi Order Guard - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    print(f"Guarding: {guard_label} strategy orders")
    if dry_run:
        print("[DRY RUN MODE - No orders will be cancelled]")
    print(f"Cancel buffer: {args.cancel_buffer} minutes")
    print(f"Min confidence: {args.min_confidence}")
    if not args.once:
        print(f"Poll interval: {args.poll_interval} minutes")
    print(f"Log: {log_file}")
    print()

    logger.info(
        f"Guard started: strategies={guarded or 'all'}, dry_run={dry_run}, "
        f"cancel_buffer={args.cancel_buffer}min, min_confidence={args.min_confidence}, "
        f"once={args.once}, poll_interval={args.poll_interval}min"
    )

    # Load private key
    try:
        with open(global_config.private_key_path, "r") as f:
            private_key_pem = f.read()
    except Exception as e:
        print(f"Error reading private key: {e}")
        return 1

    client = KalshiClient(
        api_key_id=global_config.api_key_id,
        private_key_pem=private_key_pem,
        base_url=global_config.api_base_url,
    )

    db = Database(global_config.database_path)

    guard = OrderGuard(
        client=client,
        db=db,
        grok_api_key=global_config.grok_api_key,
        cancel_buffer_minutes=args.cancel_buffer,
        min_confidence=args.min_confidence,
        dry_run=dry_run,
        strategies=guarded or None,
    )

    try:
        while True:
            cycle_time = datetime.now().strftime("%H:%M:%S")
            print(f"[{cycle_time}] Running guard cycle...")

            stats = guard.run_cycle()

            filled = stats.get("filled_contracts", 0)
            filled_str = f" ({filled} already filled)" if filled else ""
            errors_str = f", Errors: {stats['errors']}" if stats.get("errors") else ""
            print(
                f"[{cycle_time}] "
                f"Resting: {stats['resting_orders']}, "
                f"Events checked: {stats['events_checked']}, "
                f"Cancelled: {stats['orders_cancelled']}{filled_str}"
                f"{errors_str}"
            )
            logger.info(
                f"Guard cycle complete: resting={stats['resting_orders']}, "
                f"events_checked={stats['events_checked']}, "
                f"cancelled={stats['orders_cancelled']}, "
                f"filled_contracts={filled}, errors={stats.get('errors', 0)}"
            )

            if args.once:
                break

            print(f"Sleeping {args.poll_interval} minutes...")
            time.sleep(args.poll_interval * 60)
    except KeyboardInterrupt:
        print()
        print("Guard stopped.")
    finally:
        db.close()

    return 0


def main() -> int:
    """Main entry point."""
    load_dotenv()

    args = parse_args()

    if args.list_strategies:
        return run_list_strategies()
    elif args.performance is not None:
        return run_performance(args.performance)
    elif args.check_settlements:
        return run_check_settlements()
    elif args.guard:
        return run_guard(args)
    elif args.run:
        return run_continuous(args)
    elif args.strategy:
        return run_strategy(args.strategy, args.config, args.dry_run, args.confirm)
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
