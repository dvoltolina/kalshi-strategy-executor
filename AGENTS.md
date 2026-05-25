# Agent Rules

This repo is a real-money trading bot. When operated with `DRY_RUN=false`, every code path that touches `OrderPlacer` can move money on Kalshi. These rules exist to keep autonomous and semi-autonomous coding agents from accidentally enabling unsafe behavior.

## Start Of Task Checklist

1. Read:
   - `README.md`
   - `AGENTS.md` (this file)
   - `ARCHITECTURE.md`
   - The current `CHANGELOG_*` for today's date, if any
   - Any prompt pack relevant to the workstream (under `prompts/`)
   - The YAML(s) under `strategies/` for any strategy you will touch
2. Inspect:
   - `git branch --show-current`
   - `git status --short`
   - `git log --oneline -5`
   - `grep -E '^(DRY_RUN|MAX_TOTAL_NOTIONAL_USD)=' .env` to know live-money posture
3. Restate scope internally before editing:
   - Files expected to change
   - Whether the change affects order placement, cancellation, or balance arithmetic
   - Whether the change affects auth, API keys, or PEM handling
   - Testing approach

## Live-Money Discipline

- **Never flip `DRY_RUN=false` as an agent action.** Only the human operator may do that, and only after explicit confirmation in the conversation. If a task asks you to "deploy live," surface the diff, the worst-case exposure math, and the verification you ran, then wait for the human to flip `DRY_RUN` themselves.
- **Never raise `MAX_TOTAL_NOTIONAL_USD` to "make a test pass."** If a test trips the cap, the test or sizing is wrong, not the cap.
- **Never weaken or remove guard checks** (`OrderGuard`, event-start fallbacks, `min_confidence`, `cancel_buffer_minutes`) as a shortcut. Guard rejections are intentional safety; investigate before bypassing.
- **Never bypass `post_only`** unless a prompt explicitly requires a market-style execution test and the human has authorized it for that scope.
- **Never skip reconciliation** at runner startup. The local DB must catch up to Kalshi state before any new orders are placed.

## API Key & Secret Discipline

- `.env`, `*.pem`, and any private key path must stay gitignored.
- Never `cat`, `Read`, or log the contents of `KALSHI_PRIVATE_KEY_PATH`. Inspect only existence and metadata.
- `XAI_API_KEY` is needed for guard fallback. Treat it as a secret — never echo it in logs, screenshots, or commit messages.
- PnL reports (`pnl_reports/`) contain account data. Stay gitignored.

## Scope Discipline

- Prefer small vertical slices over broad rewrites. A risk-cap change should not also "tidy up" unrelated modules.
- Avoid editing files outside the active prompt's scope.
- If two prompts touch the same file, execute them serially and document the dependency in the prompt pack `INDEX.md`.
- New strategies must inherit `BaseStrategy` from `src/strategies/base.py` and ship a matching YAML under `strategies/`.
- The `OrderPlacer.place_order_from_params` path is the choke point for live orders. Any new "convenience" path that creates orders without going through it is a regression and must be rejected at review.

## Risk Surface Rules

- Every order-placement call site must respect the global notional cap. Currently this means consulting the `OrderBudget` injected into `OrderPlacer`. Adding a new order site without budget enforcement is a release blocker.
- Strategy YAMLs must keep `contracts_per_market` sized so a single full-ladder fill stays within `MAX_TOTAL_NOTIONAL_USD`. Reviewers should check this arithmetic, not trust the author.
- Per-ticker dedup (already-traded + portfolio commitment check) is necessary but **not sufficient**. The dollar cap is the backstop.
- Kalshi's exchange-side balance check is a final backstop, not a primary control. Do not rely on it.

## Documentation Requirements

Update `ARCHITECTURE.md` whenever:

- A new strategy plugin or module is introduced.
- The order-placement, reconciliation, guard, or settlement flow changes shape.
- The data model (SQLite schema) changes.
- A new external integration is added (e.g., another LLM provider, another exchange).

Update or create a `CHANGELOG_<WORKSTREAM>_<YYYY_MM_DD>.md` whenever:

- A risk control is added, removed, loosened, or tightened.
- Default sizing in a strategy YAML changes.
- A new env var becomes required for safe operation.
- A bug fix lands in any code path that runs in `--run` continuous mode.

Update `README.md` only when user-visible CLI surface or env var requirements change.

## Testing Requirements

- `uv run --all-extras pytest tests/ -v` must pass before any commit that touches `src/`.
- Risk-cap and budget arithmetic changes require unit tests with explicit numeric assertions, not just "happy path" runs.
- Manual dry-run (`uv run python -m src.main --run --dry-run`) is required before any change that affects the continuous loop.

## Commit Discipline

- One coherent change per commit. Risk caps, sizing edits, and cert/SSL fixes are separate commits.
- Commit messages: short imperative summary, then a paragraph on _why_ and _what risk it addresses_.
- Push feature branches to `origin` for review; do not push to `main` directly when the change affects order placement.
