# Kalshi Strategy Executor

A configurable Python trading bot for Kalshi prediction markets. It discovers
markets, evaluates strategy-specific filters, places post-only limit orders,
tracks orders in SQLite, reconciles fills and cancellations, and computes
settlement performance.

The project is designed around small strategy plugins backed by YAML config
files. It can run a single strategy once, run continuously on a clock-aligned
loop, run only the order guard, check settlements, manage Kalshi subaccounts,
and generate an auditable PnL report from exchange records.

## Important Risk Notice

This is experimental trading software. When `DRY_RUN=false`, it can place and
cancel real-money orders through your Kalshi account. Review the strategy YAML,
contract counts, price limits, guard settings, and subaccount selection before
running it with funded credentials.

Nothing in this repository is financial advice. You are responsible for API
keys, private keys, account funding, strategy risk, exchange rules, and any
orders placed by the bot.

## What It Does

- Runs multiple strategies from `src/strategies/`.
- Uses YAML files in `strategies/` for strategy configuration.
- Authenticates to Kalshi with API key ID plus RSA private key.
- Supports dry-run and interactive confirmation modes.
- Places post-only limit orders to avoid crossing the spread.
- Stores orders, market snapshots, event estimates, and settlements in SQLite.
- Deduplicates against markets already traded by the bot.
- Guards time-sensitive markets by canceling resting orders before events start.
- Reconciles startup state against Kalshi so local DB state catches up after restarts.
- Uses WebSocket events for real-time fills and settlements when available.
- Falls back to REST polling if WebSocket is unavailable.
- Provides settlement and performance summaries.
- Generates reproducible PnL reports from Kalshi fills and settlements.

## Repository Layout

```text
src/
  main.py                 CLI entry point
  config.py               Environment config loader and validation
  kalshi_client.py        Authenticated Kalshi REST client
  database.py             SQLite persistence and migrations
  runner.py               Continuous runner for trade/guard/settle loop
  ws_client.py            WebSocket fill and settlement listener
  reconciler.py           Startup reconciliation
  order_guard.py          Event-start safety guard and cancellations
  order_placer.py         Limit order placement
  settlement_checker.py   Settlement and PnL updates
  market_utils.py         Shared market filters and price normalization
  strategies/
    base.py               Strategy interface and OrderParams
    mentions.py           Mentions strategy
    longshot.py           Longshot strategy

strategies/
  mentions.yaml           Mentions strategy config
  longshot.yaml           Longshot strategy config

scripts/
  subaccount.py           Kalshi subaccount helper
  pnl_report.py           Exchange-sourced PnL report generator

tests/                    Pytest suite
```

## Requirements

- Python 3.10+
- `uv` for dependency management
- A Kalshi API key ID and matching private key PEM file
- `XAI_API_KEY` for guarded trading and event start-time fallback checks

The bot defaults to Kalshi production:

```text
https://api.elections.kalshi.com/trade-api/v2
```

You can override that with `KALSHI_API_BASE_URL`.

## Installation

1. Install dependencies:

   ```bash
   uv sync
   ```

2. Create your environment file:

   ```bash
   cp .env.example .env
   ```

   On PowerShell:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Edit `.env`:

   ```dotenv
   KALSHI_API_KEY_ID=your-api-key-id
   KALSHI_PRIVATE_KEY_PATH=/absolute/or/relative/path/to/private-key.pem
   XAI_API_KEY=
   DRY_RUN=true
   LOG_LEVEL=INFO
   DATABASE_PATH=data/orders.db
   ```

4. Confirm the bot can load:

   ```bash
   uv run python -m src.main --list-strategies
   ```

Keep `DRY_RUN=true` until you have inspected the strategy configs and verified
the output against your expectations.

## Configuration

Global config comes from `.env`.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `KALSHI_API_KEY_ID` | Yes | none | Kalshi API key ID. |
| `KALSHI_PRIVATE_KEY_PATH` | Yes | none | Path to the private key PEM for the API key. |
| `KALSHI_API_BASE_URL` | No | Kalshi production URL | REST API base URL. |
| `XAI_API_KEY` | Yes for trading/guard | none | xAI key used by the guard when Kalshi milestones do not provide start times. |
| `DRY_RUN` | No | `false` in code, `true` in `.env.example` | If true, no live orders or cancellations are sent. |
| `MAX_TOTAL_NOTIONAL_USD` | **Yes when `DRY_RUN=false`** | none | Global cap on outstanding USD exposure (resting orders + held positions). Startup raises `ConfigError` if missing in live mode. Minimum 0.01. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `DATABASE_PATH` | No | `data/orders.db` | SQLite database path. |
| `SUBACCOUNT_NUMBER` | No | none | Optional subaccount number, 1-32. |

Legacy single-strategy variables such as `CONTRACT_COUNT`, `MIN_YES_PRICE`,
`MAX_YES_PRICE`, and `SPORTS_CATEGORIES` are still parsed for compatibility,
but strategy YAML files are the main configuration surface.

## Strategy Configs

Strategies are configured in YAML under `strategies/`.

Common top-level fields:

```yaml
name: mentions
description: Buy NO on Mention markets with volume > 500
enabled: true
guard: true
skip_already_traded: true
```

- `enabled`: used by continuous mode to decide what runs by default.
- `guard`: opts the strategy into the order guard.
- `skip_already_traded`: avoids placing a new order on markets with pending
  bot orders.

### Mentions Strategy

Config: `strategies/mentions.yaml`

The mentions strategy buys NO on "mentions" markets, such as markets asking
whether a commentator or public figure will say a phrase.

Current defaults:

- Enabled in continuous mode.
- Guarded by default.
- Searches markets closing in 0-30 hours.
- Filters by Mentions category and Mentions tag.
- Excludes disqualified/NQE markets and configured keywords.
- Requires NO bid between 20c and 85c.
- Places a three-level ladder of post-only NO buy orders.
- Allocates `contracts_per_market` across the ladder.
- Current default `contracts_per_market` is 5 (sized for a small funded account; raise for larger balances).

Example:

```yaml
filters:
  categories:
    - Mentions
  tags:
    - Mentions
  min_no_bid_price: 0.20
  max_no_bid_price: 0.85
  min_close_hours: 0
  max_close_hours: 30

guard: true

order:
  side: "no"
  contracts_per_market: 5
  pricing:
    mode: ladder
    levels: 3
    start_offset: 0
    step: -1
  post_only: true
```

Ladder mode starts at the current best NO bid plus `start_offset`, then steps
by `step` cents for each level. With `step: -1`, the ladder moves downward
from the best bid, providing resting depth at lower NO prices.

Worst-case dollar exposure per market is `contracts_per_market * max_no_bid_price`.
With the defaults above and `max_no_bid_price: 0.85`, that is $4.25 per market.
Always sanity-check this number against your `MAX_TOTAL_NOTIONAL_USD` cap.

### Longshot Strategy

Config: `strategies/longshot.yaml`

The longshot strategy buys NO on sports markets where YES is priced as a
longshot.

Current defaults:

- Disabled in continuous mode.
- Searches open Sports/NFL/NBA/MLB/NHL markets.
- Excludes mention tickers.
- Excludes multivariate markets.
- Requires YES bid between 2c and 19c.
- Places one post-only NO buy order at current NO bid.
- Current default `contracts_per_market` is 10.

Example:

```yaml
filters:
  categories:
    - Sports
    - NFL
    - NBA
    - MLB
    - NHL
  min_yes_price: 0.02
  max_yes_price: 0.19
  exclude_tickers_containing:
    - MENTION
  exclude_multivariate: true

order:
  side: "no"
  contracts_per_market: 10
  pricing:
    mode: at_bid
  post_only: true
```

## Running the Bot

All main commands go through:

```bash
uv run python -m src.main <mode>
```

Only one mode can be selected at a time.

### List Strategies

```bash
uv run python -m src.main --list-strategies
```

### Going Live — Safe First Run

Recommended ramp before letting the continuous runner go unsupervised:

1. Keep `DRY_RUN=true` and watch a full hour of `uv run python -m src.main --run --dry-run`. Verify the `Budget: cap=$X.XX ...` line appears at startup and at each trade tick.
2. Flip `DRY_RUN=false` in `.env`. Confirm `MAX_TOTAL_NOTIONAL_USD` is set; startup will fail fast if it isn't.
3. Do a single hand-confirmed order first: `uv run python -m src.main --strategy mentions --confirm`. The bot prints each order plus your remaining budget; type `n` on anything you don't want placed.
4. Inspect the resting order on Kalshi's UI. Wait for either a fill, a cancellation by the guard, or expiry.
5. Only once you're satisfied with that one round-trip, switch to `--run`.

This bot has no daily loss limit, no stop-loss, and no "max trades per day" — `MAX_TOTAL_NOTIONAL_USD` is the only automated brake on outstanding exposure.

### One-Shot Strategy Run

Run one strategy once, scan for markets, check event start times, and place
orders or dry-run orders.

```bash
uv run python -m src.main --strategy mentions
```

Force dry-run regardless of `.env`:

```bash
uv run python -m src.main --strategy mentions --dry-run
```

Prompt before each order:

```bash
uv run python -m src.main --strategy mentions --confirm
```

Use a custom config file:

```bash
uv run python -m src.main --strategy mentions --config strategies/mentions.yaml
```

One-shot trading requires `XAI_API_KEY`. Before placing orders, it warms the
event-start cache and blocks markets whose event has started, is inside the
cancel buffer, or cannot be estimated.

### Continuous Runner

Continuous mode is the normal long-running process.

```bash
uv run python -m src.main --run
```

Dry-run continuous mode:

```bash
uv run python -m src.main --run --dry-run
```

Enable disabled strategies from the CLI:

```bash
uv run python -m src.main --run --longshot
uv run python -m src.main --run --mentions --longshot
```

Startup flow:

1. Reconcile pending orders against Kalshi.
2. Reconcile settlements.
3. Start WebSocket fill and settlement listener.
4. Initialize the order guard for strategies with `guard: true`.
5. Warm the guard cache using current portfolio positions.
6. Enter a five-minute tick loop.

Tick schedule:

- Every five minutes: process WebSocket events, then run the guard.
- At :00 and :30: guard, reprice, trade, then settle.
- If WebSocket disconnects: reconnect with backoff and reconcile after reconnect.
- If WebSocket is unavailable: continue in REST-only mode.

### Order Guard Only

The guard protects resting orders from filling after the underlying event has
started or is about to start.

Run once:

```bash
uv run python -m src.main --guard --once --dry-run
```

Run continuously:

```bash
uv run python -m src.main --guard
```

Custom interval and buffer:

```bash
uv run python -m src.main --guard --poll-interval 5 --cancel-buffer 60 --min-confidence medium
```

Guard inputs:

- Resting Kalshi orders.
- Local DB order IDs, so it only guards bot orders for opted-in strategies.
- Kalshi market and event metadata.
- Kalshi milestones API.
- Grok/xAI fallback when milestones do not provide a usable start time.

If neither milestones nor Grok can estimate an event start time, the guard
treats the event as unsafe and cancels guarded resting orders as a precaution.

### Settlements

Record settlement outcomes for unsettled bot orders:

```bash
uv run python -m src.main --check-settlements
```

Show overall performance:

```bash
uv run python -m src.main --performance
```

Filter by strategy:

```bash
uv run python -m src.main --performance mentions
```

Performance uses local DB records. For a report sourced directly from Kalshi
exchange records, use `scripts/pnl_report.py`.

## PnL Reporting

Generate an auditable report from Kalshi fills, settlements, and balance:

```bash
uv run python scripts/pnl_report.py
```

The report script:

- Reads bot order IDs from the local SQLite DB.
- Pulls fills and settlements from the Kalshi API.
- Filters fills to bot order IDs.
- Computes per-market PnL from fill prices and settlement outcomes.
- Saves raw API responses and report files under `pnl_reports/<timestamp>/`.

`pnl_reports/` is gitignored because it can contain account data.

## Subaccount Management

The helper script wraps Kalshi subaccount endpoints:

```bash
uv run python scripts/subaccount.py create
uv run python scripts/subaccount.py list
uv run python scripts/subaccount.py balance 1
uv run python scripts/subaccount.py transfer 0 1 100
uv run python scripts/subaccount.py transfers --limit 20
uv run python scripts/subaccount.py positions 1
```

Add `--json` before the command for JSON output:

```bash
uv run python scripts/subaccount.py --json list
```

## How Orders Are Placed

1. Strategy fetches open markets from Kalshi.
2. Market prices are normalized to cents.
3. Strategy filters markets by category, tags, prices, volume, close time, and
   strategy-specific exclusions.
4. Deduplication removes markets with pending bot orders.
5. Guard checks event start timing.
6. Strategy builds one or more `OrderParams`.
7. `OrderPlacer` sends post-only limit orders unless dry-run is enabled.
8. Live orders are recorded in SQLite with market snapshots.
9. Later reconciliation, WebSocket events, guard cancellations, and settlement
   checks update order status and PnL.

## Data, Logs, and Generated Files

The bot writes local runtime state:

| Path | Purpose | Git status |
| --- | --- | --- |
| `data/orders.db` | SQLite order and settlement database | ignored |
| `logs/bot_*.log` | Timestamped logs | ignored |
| `state/` | Resume/runtime state, if used | ignored |
| `pnl_reports/` | Generated PnL reports and raw API responses | ignored |

Do not commit real `.env` files, private keys, databases, logs, or PnL reports.

## Development

Install development dependencies:

```bash
uv sync --all-extras
```

Run tests:

```bash
uv run --all-extras pytest tests/ -v
```

Run a single test file:

```bash
uv run --all-extras pytest tests/test_database.py -v
```

Check the lockfile:

```bash
uv lock --check
```

## Creating a New Strategy

1. Create `src/strategies/mystrategy.py`.
2. Define a class that inherits `BaseStrategy`.
3. Implement `find_markets()`.
4. Implement `calculate_order()` for one order per market, or
   `calculate_orders()` for ladders/multiple orders.
5. Create `strategies/mystrategy.yaml`.
6. Add `enabled: true` if it should run in continuous mode by default.
7. Add `guard: true` if resting orders need event-start cancellation.
8. Run it:

   ```bash
   uv run python -m src.main --strategy mystrategy --dry-run
   ```

The abstract interface is in `src/strategies/base.py`.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `KALSHI_API_KEY_ID is required` | `.env` missing or not loaded | Create `.env` and set the key ID. |
| `Private key file not found` | Bad `KALSHI_PRIVATE_KEY_PATH` | Use a valid path to your PEM file. |
| `XAI_API_KEY is required` | Guarded trading needs event timing fallback | Set `XAI_API_KEY` or only use non-trading modes. |
| No qualifying markets | Filters are too strict or no current opportunities | Inspect strategy YAML and logs. |
| Orders are skipped near event start | Guard is blocking unsafe markets | Increase caution is expected; review logs. |
| WebSocket unavailable | `websocket-client` missing or connection failed | Run `uv sync`; runner will continue via REST. |
| YAML parses `no` as boolean | Unquoted YAML value | Always write `side: "no"` or `side: "yes"`. |
| Post-only order rejected | Price would cross the spread | Use `at_bid`, lower offsets, or keep ladder below ask. |

## License

MIT. See `LICENSE`.
