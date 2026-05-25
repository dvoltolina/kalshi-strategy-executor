# CHANGELOG — Risk Caps — 2026-05-25

## Summary

Prepared this fork for the first live (DRY_RUN=false) deployment against a $90 funded Kalshi account. Adds a global account-level USD exposure cap (`MAX_TOTAL_NOTIONAL_USD`), enforces it inside the single order-placement choke point (`OrderPlacer.place_order_from_params`), lowers the default mentions strategy sizing so a single full ladder cannot exceed ~$5 per market, and fixes the macOS Python 3.14 SSL trust store so the WebSocket connects cleanly. Adds AGENTS.md, ARCHITECTURE.md, and a `prompts/risk_caps/` prompt pack so future autonomous agents have written guardrails.

The flip of `DRY_RUN=false` is intentionally NOT part of this changelog — that remains an operator action after diff review on `feature/risk-caps`.

## Changes

### Added

- `MAX_TOTAL_NOTIONAL_USD` env var. REQUIRED when `DRY_RUN=false`; `load_config()` raises `ConfigError` at startup if missing. Minimum 0.01 (values below that round to cents = 0 = no-cap sentinel, so they are rejected).
- `src/order_budget.py` — new module with:
  - `OrderBudget` dataclass: `cap_cents`, `committed_cents`, `reserved_cents`, methods `try_reserve`, `release`, `release_committed`.
  - `BudgetSnapshotError` exception.
  - `snapshot_committed_cents(client)` — sums Kalshi positions + resting orders. Conservative fallbacks (`count × 100c`, `remaining × 100c`) when Kalshi fields are missing.
  - `build_budget(client, cap_usd)` — constructs the budget from a fresh snapshot.
  - `order_notional_cents(order_dict)` — helper for releasing committed cents on cancel.
- `tests/test_order_budget.py` — 24 tests covering arithmetic, no-cap sentinel, snapshot pagination, fallbacks, API failure propagation.
- `tests/test_order_placer.py::TestOrderPlacerBudgetEnforcement` — 7 tests covering reject/reserve/release/exhaustion semantics.
- New `test_config.py` tests for the live-mode requirement, sub-cent rejection, and value validation.
- `AGENTS.md` — agent rules: live-money discipline, API key handling, scope discipline, documentation requirements, commit discipline.
- `ARCHITECTURE.md` — modules, stack, process modes, continuous loop, order lifecycle, risk surface (8 controls), data model, plugin contract, configuration.
- `prompts/risk_caps/INDEX.md` and four task prompts (`01_account_budget.md`, `02_mentions_sizing.md`, `03_macos_ssl_fix.md`, `04_changelog_and_env_example.md`).
- README sections: env-var row for `MAX_TOTAL_NOTIONAL_USD`; "Going Live — Safe First Run" subsection with a ramp procedure.

### Changed

- `strategies/mentions.yaml`: `contracts_per_market` 500 → 5, `levels` 7 → 3. Front-heavy geometric allocation now distributes as 3/1/1 across 3 levels.
- `src/order_placer.py::OrderPlacer.__init__`: accepts optional `budget: OrderBudget`.
- `src/order_placer.py::place_order_from_params`: pre-flight `budget.try_reserve(notional_cents)`. On `KalshiAPIError` or unexpected exception, `budget.release(notional_cents)` so the next caller sees freed capacity. Dry-run reservations stay (simulates a resting order).
- `src/runner.py::_run_trading`: builds per-tick budget via `_build_order_budget()` and injects into the OrderPlacer. Aborts the trading cycle on `BudgetSnapshotError` (fail-closed).
- `src/runner.py::_run_reprice`: same per-tick budget; after each successful cancel, calls `budget.release_committed(order_notional_cents(order))` so the replacement can actually fit (fixes blocking audit finding 1).
- `src/runner.py::Runner.start`: adds a startup budget log line so the operator sees `Budget: cap=$X, committed=$Y, remaining=$Z` immediately, not at the first :00/:30 trade tick.
- `src/main.py::run_strategy`: same budget injection for the one-shot CLI path. Uses the shared `build_budget` helper (was duplicated; cleaned up).
- `src/main.py::run_strategy --confirm`: prompts now show remaining budget per order.
- `src/config.py::load_config`: validates `MAX_TOTAL_NOTIONAL_USD` (numeric, ≥ 0.01, required when not dry-run).
- `.env.example`: documents `MAX_TOTAL_NOTIONAL_USD=45` with comment block.
- `.gitignore`: adds `.DS_Store`.
- README env-var table + "Mentions Strategy" example reflect new sizing.

### Fixed

- macOS Python 3.14 SSL trust store: ran `/Applications/Python 3.14/Install Certificates.command` on the deployment host so `ssl.get_default_verify_paths().cafile` resolves to the certifi bundle. Eliminates the `[SSL: CERTIFICATE_VERIFY_FAILED]` reconnect loop in WebSocket startup. No repo change; host action only.

## Risk Math

| Setting | Value |
|---|---|
| Account balance | $90 |
| `MAX_TOTAL_NOTIONAL_USD` | $45 |
| Mentions `contracts_per_market` | 5 |
| Mentions `levels` | 3 |
| Mentions `max_no_bid_price` | $0.85 |
| Worst-case per market | 5 × $0.85 = **$4.25** |
| Markets that fit under cap | up to 10 simultaneous mentions placements |

## Verification

- `uv run --all-extras pytest tests/ -v` → **345 passed** (was 311 baseline + 34 new).
- `uv run python -m src.main --list-strategies` → succeeds, lists `mentions` and `longshot`.
- `uv run python -m src.main --strategy mentions --dry-run` → runs cleanly; 0 qualifying markets at run time but the path through scanner + filter + would-have-placed logging is exercised.
- `uv run python -m src.main --run --dry-run` → reconciles, connects WebSocket (`Websocket connected`, no SSL errors), prints `Budget: cap=$45.00, committed=$0.00, remaining=$45.00` before tick loop, enters tick loop, clean shutdown.
- macOS TLS: `ssl.get_default_verify_paths().cafile` resolves; TLS smoke test against `api.elections.kalshi.com:443` succeeds with TLSv1.3.

## Audit Findings

Three audit agents ran in parallel against the diff: code correctness, security/secrets, and docs/product UX.

**Code correctness (general-purpose agent):**

- **BLOCKING 1** — Reprice budget snapshot double-counts about-to-be-cancelled orders → fixed via `OrderBudget.release_committed` after each successful cancel.
- **BLOCKING 2** — `int(cap_usd * 100)` for sub-cent values rounds to 0 (no-cap sentinel) → fixed by requiring `>= 0.01` in `load_config`.
- **BLOCKING 3** — Silent `break` on first paginate exception in `_estimate_committed_notional_cents` would yield `committed=0` and over-deploy → fixed by raising `BudgetSnapshotError` and aborting the cycle.
- **NON-BLOCKING 4** — Kalshi field names (`market_exposure`, `no_price`, `yes_price`) inferred from spec, not cross-checked against a real position. Mitigation: conservative `count × 100c` fallback. Listed under Residual Risk.
- **NON-BLOCKING 5** — Duplicate budget builder between `main.py` and `runner.py` → fixed by extracting `build_budget` to `order_budget.py`.
- **NON-BLOCKING 6** — Lack of tests for `snapshot_committed_cents` / `build_budget` → fixed with new test classes.
- **NON-BLOCKING 7, 8** — Edge cases around retry interaction and `OrderParams` invariants → accepted as-is; documented constraints.

**Security/secrets (general-purpose agent):**

- BLOCKING: none.
- All findings non-blocking; new log lines emit only dollar amounts and ticker text, no secret material. `.gitignore` still covers `.env`, `*.pem`, `data/`, `state/`, `pnl_reports/`, `logs/`. Test fixtures use placeholder values.
- **RESIDUAL RISK** — During this session, `.env` was read into the conversation transcript when appending `MAX_TOTAL_NOTIONAL_USD=45`, which means the `XAI_API_KEY` value appears in the transcript. The operator should consider rotating `XAI_API_KEY` at console.x.ai if the transcript is stored/synced beyond their local machine. `KALSHI_API_KEY_ID` and the PEM file were not read.

**Docs/product UX (general-purpose agent):**

- **BLOCKING** — `CHANGELOG_RISK_CAPS_2026_05_25.md` missing → this document fixes that.
- **BLOCKING** — README env-var table missing `MAX_TOTAL_NOTIONAL_USD`; mentions example still showed 500 / 7 → fixed.
- **NON-BLOCKING** — No startup budget log for continuous mode → fixed (`Runner.start` now logs it).
- **NON-BLOCKING** — `--confirm` didn't show remaining budget → fixed.
- **NON-BLOCKING** — "Budget exhausted" only at WARNING level, easy to miss → fixed (now also `print()`s).
- **NON-BLOCKING** — No written "safe first live run" guidance → fixed (new README subsection).

## Known Limitations

- **No per-strategy budget split.** A single global cap; if mentions consumes all of it, longshot gets nothing.
- **No daily loss limit, no stop-loss, no max-trades-per-day.** Only outstanding-exposure cap.
- **Cap is a static env var**, not a percentage of live balance. If you transfer funds in or out, the cap doesn't change automatically.
- **Budget is refreshed per trade tick (5 min cadence).** Partial fills within a tick don't refund budget until the next snapshot. Acceptable at $45 / $4.25-per-market sizing.
- **Kalshi field names assumed**, not API-doc-verified. `market_exposure`, `no_price`, `yes_price` are likely correct based on the codebase, but if Kalshi returns these as strings or under different keys, the conservative `count × 100c` fallback engages and tightens the cap. A first live run with `--confirm` will surface any discrepancy via the "Budget remaining" line.
- **Reprice phase runs before trading** in `_tick`. They use separate budget instances. In-flight reservations from one phase do not carry to the other; only the Kalshi-reported state does (so a reprice replacement that was placed will appear as a resting order in the trading-phase snapshot, but a reprice _reservation_ that hasn't placed yet will not). Order of phases means trading always sees post-reprice state. Acceptable for current sizing.

## Next Steps

1. **Operator**: review the full diff on `feature/risk-caps` against `main`. Pay particular attention to:
   - `src/order_placer.py` — the budget reservation/release block.
   - `src/runner.py::_run_trading` and `::_run_reprice` — budget construction and fail-closed behavior.
   - `strategies/mentions.yaml` — verify the sizing matches your intent.
   - `.env` — confirm `MAX_TOTAL_NOTIONAL_USD=45` and that `DRY_RUN` is still `true`.
2. **Optional but recommended**: open a PR on GitHub for archival review:
   `https://github.com/dvoltolina/kalshi-strategy-executor/pull/new/feature/risk-caps`
3. **Merge to main** (or keep on the feature branch) when satisfied.
4. **Flip `DRY_RUN=false`** in `.env` only after step 1.
5. **Safe first live run** (also documented in README):
   - `uv run python -m src.main --strategy mentions --confirm`
   - Hand-confirm a single small order; verify "Budget remaining" shows expected value.
   - Inspect the resting order on Kalshi's UI.
   - Wait for fill, guard cancel, or expiry.
6. After one clean round-trip, switch to `uv run python -m src.main --run`.
7. Tail `logs/bot_*.log` during initial live runs; watch for unexpected `Order rejected by budget` lines or `BudgetSnapshotError`.
8. **Residual risk reminder**: consider rotating `XAI_API_KEY` per the security audit if the conversation transcript was stored or synced.
