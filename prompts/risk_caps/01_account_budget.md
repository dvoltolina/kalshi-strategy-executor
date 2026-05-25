# 01 — Account-Level Dollar Exposure Cap

## Task

Introduce a global account-level dollar cap on outstanding Kalshi exposure (resting orders + held positions × cost basis), enforced inside `OrderPlacer.place_order_from_params` so both the one-shot and continuous-runner order paths benefit. Cap is configured via env var `MAX_TOTAL_NOTIONAL_USD`.

## Context

The bot currently has no global $ guardrail. The only check is per-ticker `contracts_per_market`, which doesn't aggregate across tickers. For a $90 funded account, mentions at default 500 contracts × $0.85 = $425 per market would obliterate the balance on a single fill. This task adds the missing global cap.

## Required Reading

Before editing, read:

- `AGENTS.md` — "Live-Money Discipline" and "Risk Surface Rules" sections.
- `ARCHITECTURE.md` — "Risk Surface" and "Order Lifecycle" sections.
- `src/config.py` — current Config dataclass and `load_config()`.
- `src/order_placer.py` — current `OrderPlacer` and `place_order_from_params`.
- `src/runner.py` — `_run_trading`, `_run_single_strategy`, `_get_portfolio_commitment`.
- `src/main.py::run_strategy` — the one-shot path (lines ~131-380).
- `tests/test_order_placer.py` — existing test patterns to extend.

## Scope Boundaries

May modify:

- `src/config.py`
- `src/order_placer.py`
- `src/runner.py`
- `src/main.py` (one-shot path only — do not change CLI parser or unrelated modes)
- `.env.example`
- `tests/test_order_placer.py`

May create:

- `src/order_budget.py`
- `tests/test_order_budget.py`

Off-limits:

- `src/strategies/*` — strategy plugin code stays untouched.
- `src/kalshi_client.py` — REST client is correct.
- `src/database.py` — no schema changes.
- `src/order_guard.py`, `src/ws_client.py`, `src/reconciler.py`, `src/settlement_checker.py` — orthogonal modules.

## Dependencies

None.

## Implementation Notes

### `OrderBudget` (new, `src/order_budget.py`)

A small, pure-Python class. No I/O of its own.

```python
@dataclass
class OrderBudget:
    cap_cents: int                  # MAX_TOTAL_NOTIONAL_USD * 100
    committed_cents: int            # current snapshot: positions + resting orders
    reserved_cents: int = 0         # incremented per successful place

    @property
    def remaining_cents(self) -> int:
        return max(0, self.cap_cents - self.committed_cents - self.reserved_cents)

    def try_reserve(self, notional_cents: int) -> bool:
        if notional_cents <= 0:
            return False  # invariant violation; caller bug
        if notional_cents > self.remaining_cents:
            return False
        self.reserved_cents += notional_cents
        return True

    def release(self, notional_cents: int) -> None:
        self.reserved_cents = max(0, self.reserved_cents - notional_cents)
```

`OrderBudget.cap_cents == 0` means "no cap" — used only in dry-run when env var is unset; `try_reserve` should always succeed in that mode. Encode as: if `cap_cents == 0`, return True without mutation.

### `Config` (edit `src/config.py`)

Add to the dataclass:

```python
max_total_notional_usd: Optional[float]  # None = unlimited (dry-run only)
```

In `load_config()`:

```python
mtn_str = os.environ.get("MAX_TOTAL_NOTIONAL_USD", "").strip()
if mtn_str:
    try:
        max_total_notional_usd = float(mtn_str)
        if max_total_notional_usd <= 0:
            raise ConfigError("MAX_TOTAL_NOTIONAL_USD must be > 0")
    except ValueError:
        raise ConfigError("MAX_TOTAL_NOTIONAL_USD must be a number")
else:
    max_total_notional_usd = None

# Hard requirement when not in dry-run
if not dry_run and max_total_notional_usd is None:
    raise ConfigError(
        "MAX_TOTAL_NOTIONAL_USD must be set when DRY_RUN=false "
        "(safety cap for live trading)"
    )
```

### `OrderPlacer` (edit `src/order_placer.py`)

Accept an optional `budget: OrderBudget` in `__init__`. In `place_order_from_params`, before the dry-run branch:

```python
notional_cents = params.price_cents * params.quantity
if self.budget is not None:
    if not self.budget.try_reserve(notional_cents):
        logger.warning(
            f"Order rejected by budget: {params.ticker} "
            f"{params.quantity}x @ {params.price_cents}c "
            f"(notional=${notional_cents/100:.2f}, "
            f"remaining=${self.budget.remaining_cents/100:.2f})"
        )
        return OrderResult(
            market_ticker=params.ticker,
            success=False,
            error="account notional cap reached",
            strategy=strategy,
            price_cents=params.price_cents,
            quantity=params.quantity,
        )
```

If a live order ultimately fails after reservation (KalshiAPIError), call `self.budget.release(notional_cents)` so the budget reflects reality. Dry-run reservations stay reserved for the duration of the run — that's correct because the dry-run simulates an order that would have rested.

### `Runner` (edit `src/runner.py`)

In `_run_trading`, build the budget once per trading tick:

```python
budget = self._build_order_budget()
placer = OrderPlacer(client=self.client, dry_run=self.dry_run, budget=budget)
```

Add helper:

```python
def _build_order_budget(self) -> Optional[OrderBudget]:
    cap_usd = self.config.max_total_notional_usd
    if cap_usd is None:
        return OrderBudget(cap_cents=0, committed_cents=0)  # dry-run no-cap mode
    committed_cents = self._estimate_committed_notional_cents()
    budget = OrderBudget(cap_cents=int(cap_usd * 100), committed_cents=committed_cents)
    logger.info(
        f"Budget: cap=${cap_usd:.2f}, committed=${committed_cents/100:.2f}, "
        f"remaining=${budget.remaining_cents/100:.2f}"
    )
    if budget.remaining_cents == 0:
        logger.warning("Budget exhausted at tick start; no new orders this cycle")
    return budget

def _estimate_committed_notional_cents(self) -> int:
    """Sum cost basis of held positions + price × remaining of resting orders."""
    # Reuse the iteration pattern from _get_portfolio_commitment.
    # For positions: cost basis = position * average_price (Kalshi position record
    # provides `market_exposure` in cents already — prefer that if available).
    # For resting orders: cents = remaining_count * limit_price_cents (use no_price
    # for NO orders, yes_price for YES).
    ...
```

Read the actual Kalshi position / order JSON shapes by tracing through `_get_portfolio_commitment` and `kalshi_client.get_positions`. Prefer Kalshi's reported exposure fields over recomputing. Document any field assumptions inline.

### `main.py::run_strategy` (edit `src/main.py`)

The one-shot path also needs a budget. Add the same build + inject before the `OrderPlacer` instantiation. Reuse the helper by extracting it to a free function in `runner.py` or to `order_budget.py` — your call, but keep it deduplicated.

### `.env.example`

Add a documented entry:

```
# Maximum outstanding USD exposure (resting orders + held positions).
# REQUIRED when DRY_RUN=false. Acts as a hard cap inside OrderPlacer.
# Example for a $90 account targeting ~50% max exposure: 45
MAX_TOTAL_NOTIONAL_USD=45
```

### Tests (`tests/test_order_budget.py`, new)

Cover:

- `try_reserve` succeeds when `notional <= remaining`.
- `try_reserve` fails when `notional > remaining`.
- `try_reserve` mutates `reserved_cents` on success only.
- `release` does not go negative.
- `cap_cents == 0` always allows (no-cap dry-run mode).
- `committed_cents > cap_cents` → `remaining_cents == 0` (saturated).

### Tests (`tests/test_order_placer.py`, extend)

Add cases:

- `place_order_from_params` returns failure with `error == "account notional cap reached"` when budget rejects.
- DB recording (`db.record_order`) is **not** called when budget rejects.
- A live (non-dry-run) KalshiAPIError path calls `budget.release` so the next caller sees the freed budget.
- Existing tests still pass with `budget=None` (back-compat for any test that doesn't care).

## Acceptance Criteria

- [ ] `uv run --all-extras pytest tests/ -v` passes (all existing + new tests).
- [ ] `uv run python -m src.main --strategy mentions --dry-run` runs and (with the default mentions YAML sized down by task 02) shows budget log line.
- [ ] `uv run python -m src.main --run --dry-run` enters tick loop, prints budget summary at tick, accepts Ctrl+C cleanly.
- [ ] Removing `MAX_TOTAL_NOTIONAL_USD` from `.env` with `DRY_RUN=false` causes `ConfigError` at startup before any client init.
- [ ] No file outside the Scope Boundaries list is modified.
- [ ] All new helpers documented with docstrings; no `print` statements added outside existing patterns.

## Documentation Requirement

- Update `ARCHITECTURE.md` "Risk Surface" section if implementation differs from the spec.
- Task 04 in this pack writes the CHANGELOG; do not duplicate.

## Commit Requirement

Single commit on `feature/risk-caps`. Suggested message:

```
feat(risk): account-level $ cap via OrderBudget injected into OrderPlacer

Adds MAX_TOTAL_NOTIONAL_USD env var. Required when DRY_RUN=false.
OrderBudget reserves notional pre-flight and refunds on failed live
order. Wired through both runner and one-shot paths.
```
