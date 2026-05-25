# Kalshi Strategy Executor — Architecture

## Product

A Python trading bot for Kalshi prediction markets. It discovers markets, applies strategy-specific filters, places post-only limit orders, tracks them in SQLite, reconciles fills/cancellations, and computes settlement performance. Strategy plugins are YAML-configured Python classes.

## Stack

- Language: Python 3.10+ (Python 3.14 supported by the current `.venv`).
- Package manager: `uv` (lock file: `uv.lock`).
- Dependencies (`pyproject.toml`):
  - `requests` — Kalshi REST.
  - `cryptography` — RSA signing of API requests with the user's private key.
  - `python-dotenv` — `.env` loader.
  - `pyyaml` — strategy config files.
  - `websocket-client` — real-time fill/settlement events.
- Optional dev deps: `pytest`, `pytest-mock`.
- Storage: SQLite at `data/orders.db` (created on first write).
- External APIs:
  - Kalshi REST (`https://api.elections.kalshi.com/trade-api/v2`).
  - Kalshi WebSocket (`wss://api.elections.kalshi.com/trade-api/ws/v2`).
  - xAI / Grok for event start-time fallback when Kalshi milestones don't expose a start time.

## Repository Layout

```text
src/
  main.py                 CLI entry; dispatches one-shot, continuous, guard, settlement, perf
  config.py               Env config loader → Config dataclass; raises ConfigError early
  kalshi_client.py        Authenticated REST client (RSA-signed)
  ws_client.py            WebSocket listener for fills + settlements (background thread)
  database.py             SQLite persistence + migrations
  reconciler.py           Startup reconciliation of pending orders + unsettled positions
  runner.py               Continuous tick loop: guard / reprice / trade / settle
  order_guard.py          Event-start safety guard; cancels resting orders pre-event
  order_placer.py         OrderPlacer — single choke point for placing live orders
  market_scanner.py       Shared market query / filter primitives
  market_utils.py         Price normalization (cents vs. dollars in API responses)
  settlement_checker.py   Pulls settlement outcomes; computes per-order PnL
  logging_config.py       Centralized logging setup (file + stdout)
  state_manager.py        Resume/runtime state (rate-limit-aware operations)
  strategies/
    base.py               BaseStrategy + OrderParams dataclass
    mentions.py           "Mentions" strategy — buys NO, multi-level ladder
    longshot.py           "Longshot" strategy — buys NO on sports longshots

strategies/                 YAML configs (one per strategy)
  mentions.yaml
  longshot.yaml

scripts/
  subaccount.py           Kalshi subaccount management CLI
  pnl_report.py           Exchange-sourced PnL report generator

tests/                      Pytest suite, mirrors src/ module layout

prompts/                    Per-workstream prompt packs for autonomous agents
  <workstream>/INDEX.md
  <workstream>/<task>.md
```

## Process Modes

`src/main.py` dispatches a mutually-exclusive mode flag:

- `--list-strategies` — enumerate plugin classes + YAML descriptions.
- `--strategy <name>` — one-shot scan + place orders (with optional `--confirm`, `--dry-run`, `--config`).
- `--run` — continuous tick loop (5-min cadence; trade at :00 and :30).
- `--guard` (`--once` / loop) — order-cancellation safety guard only.
- `--check-settlements` — settle outstanding bot orders.
- `--performance [<strategy>]` — local DB summary; cost basis, PnL, ROI, per-market table.

CLI flags that override env: `--dry-run` forces dry-run regardless of `DRY_RUN` value.

## Continuous Loop (`src/runner.py`)

```
startup:
  reconcile orders → reconcile settlements → start WebSocket → init guard → warm guard cache
tick (every 5 minutes, wall-clock-aligned):
  process WS events (fills, settlement signals, reconnect-triggered reconciliation)
  run guard cycle (always)
  if minute in {00, 30}:
    reprice stale resting orders (if enabled in strategy YAML)
    run trading cycle (per discovered/enabled strategy)
    check settlements
```

WebSocket runs on a background thread and pushes events into thread-safe queues. The main loop drains the queues during `_process_ws_events()` so all DB writes stay single-threaded.

## Order Lifecycle

```
strategy.find_markets()
  → strategy.calculate_orders(market)   # returns List[OrderParams]
  → guard check (event start time, cancel buffer)
  → per-ticker portfolio commitment check (Kalshi positions + resting orders)
  → OrderBudget.try_reserve()           # global $ cap, see Risk Surface
  → OrderPlacer.place_order_from_params()
       ├─ dry_run → log "[DRY RUN] Would place order…" return synthetic OrderResult
       └─ live    → client.create_order() with post_only=True (retry on 429/5xx)
  → if success and not dry_run: db.record_order(...) status="pending"
  → reconciler / WS fill events later update status to filled / cancelled
  → settlement_checker writes outcome + pnl_cents when market settles
```

`OrderPlacer.place_order_from_params` is the **single choke point** for every live order across both one-shot and continuous modes. Any new order site must call it.

## Risk Surface

Live-money behavior is governed by:

1. **`DRY_RUN` (env)** — primary kill switch. `DRY_RUN=true` short-circuits inside `OrderPlacer` before any REST call. CLI `--dry-run` overrides env upward (only forces dry-run; cannot force live).
2. **`MAX_TOTAL_NOTIONAL_USD` (env)** — global cap on outstanding dollar exposure (resting orders + held positions × cost basis). Enforced by `OrderBudget` inside `OrderPlacer`. An order whose notional would push commitment above the cap is rejected before reaching Kalshi. _Added on `feature/risk-caps`._
3. **Per-ticker contract cap** — `contracts_per_market` in strategy YAML; runner checks live Kalshi portfolio commitment (`_get_portfolio_commitment`) and skips markets already at the cap.
4. **DB dedup** — `skip_already_traded` (default true) skips markets with any pending bot order.
5. **Order guard** — `OrderGuard` cancels resting orders before their underlying event starts. Inputs: Kalshi milestones API + xAI/Grok fallback. If neither can estimate, orders are blocked as a precaution.
6. **Post-only** — every order is `post_only=True` by default; Kalshi rejects orders that would cross the spread, eliminating accidental taker fees.
7. **Reconciliation** — startup `Reconciler.reconcile_orders()` syncs local DB to Kalshi truth so a crash + restart doesn't double-place.
8. **Exchange-side balance** — Kalshi rejects orders exceeding available balance. This is a final backstop, never a primary control.

Risk controls 1, 2, 5, 6, 7 are mandatory. 3, 4 are operational hygiene. 8 is exchange behavior we depend on but do not control.

## Data Model (SQLite)

Primary tables (see `src/database.py` for full schema and migrations):

- `orders` — every bot-placed order; `client_order_id`, `order_id`, `strategy`, `ticker`, `side`, `action`, `price_cents`, `quantity`, `status`, `market_snapshot` (JSON), timestamps.
- `settlements` — per-market settlement outcome + computed `pnl_cents`.
- `event_estimates` — cached event start-time predictions (guard).
- `market_snapshots` — historical price snapshots at order time.

User account data (positions, balance, fills history) is **not persisted** beyond what's needed for reconciliation. Authoritative state always comes from Kalshi.

## Strategy Plugin Contract

A strategy is a Python class under `src/strategies/<name>.py` that:

1. Inherits `BaseStrategy` from `src/strategies/base.py`.
2. Implements `find_markets()` → `List[Dict[str, Any]]`.
3. Implements `calculate_order(market)` → `Optional[OrderParams]`, or overrides `calculate_orders(market)` → `List[OrderParams]` for multi-order strategies (ladders).
4. Has a matching `strategies/<name>.yaml` with at minimum `name`, `enabled`, `filters`, `order`.

`OrderParams.__post_init__` validates: `1 <= price_cents <= 99`, `quantity > 0`, `side in {"yes", "no"}`.

Discovery is filesystem-based: `src/strategies/__init__.py` scans both `src/strategies/*.py` and `strategies/*.yaml`.

## Configuration

Global config (`.env`) loaded by `src/config.py::load_config()` into a `Config` dataclass at startup. Mandatory keys: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`. Trading/guard modes additionally require `XAI_API_KEY`.

Risk-control keys: `DRY_RUN`, `MAX_TOTAL_NOTIONAL_USD`.

Per-strategy config is YAML under `strategies/`. The `runner._get_portfolio_commitment()` and `OrderBudget` arithmetic always operate in cents internally; YAML values are dollars where the field is a price, and contracts where the field is a count.

## Observability

- Logs: `logs/bot_<timestamp>.log` (gitignored). Level via `LOG_LEVEL`.
- Console: structured tick-by-tick output during `--run`.
- No remote telemetry, no Sentry, no metrics endpoint. Operational monitoring is log-tailing + `--performance` summary + `scripts/pnl_report.py`.

## Compliance Boundary

This is research/experimental software operated by the account holder against their own funded Kalshi account. It is not a hosted service, not multi-tenant, and not financial advice. Disclaimers belong in `README.md` (see "Important Risk Notice"). Do not add hosted-service patterns (auth flows, rate-limited public APIs, billing) — they are out of scope.
