# 02 — Lower Mentions Sizing for $90 Account

## Task

Update `strategies/mentions.yaml` to size for a $90 funded account: drop `contracts_per_market` from 500 to 5, and `levels` from 7 to 3.

## Context

Current `mentions.yaml` is sized for a large account. Worst-case per market = 500 × $0.85 = $425, which exceeds the entire $90 balance ~5×. New target is ≤ $5 worst-case per market (5 × $0.85 max NO bid = $4.25), keeping the strategy able to span 8-10 markets under a $45 global cap.

## Required Reading

- `strategies/mentions.yaml` — current state.
- `src/strategies/mentions.py::_build_ladder` and `_ladder_quantities` — verifies how levels + total contracts allocate.
- `ARCHITECTURE.md` "Risk Surface" — confirms YAML sizing is a primary risk control.

## Scope Boundaries

May modify:

- `strategies/mentions.yaml`

Off-limits:

- All Python source.
- `strategies/longshot.yaml` — already small.
- Filters (categories, tags, price bounds) — only change `order.contracts_per_market` and `order.pricing.levels`.

## Dependencies

None.

## Implementation Notes

The geometric allocation in `mentions.py::_ladder_quantities` distributes `total_contracts` front-heavy across `levels`. With 5 total contracts and 3 levels, ratio = 0.5^(1/2) ≈ 0.707:

- weights: 1.0, 0.707, 0.5 → sum 2.207
- normalized × 5: 2.27, 1.60, 1.13
- floored: 2, 1, 1 = 4 → remainder 1 → highest fraction (2.27) gets +1
- final: 3, 1, 1 = 5 contracts across 3 levels

Worst-case per market: 5 × max_no_bid_price ($0.85) = $4.25.

Keep all other fields identical. Specifically keep:

- `name: mentions`
- `enabled: true`
- `guard: true`
- All filter values
- `pricing.mode: ladder`, `start_offset: 0`, `step: -1`
- `post_only: true`
- `reprice.enabled: false`

Final order block:

```yaml
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

## Acceptance Criteria

- [ ] `strategies/mentions.yaml` diff shows only `contracts_per_market` and `levels` changed.
- [ ] `uv run python -m src.main --strategy mentions --dry-run` runs without errors.
- [ ] Dry-run ladder log lines show ≤5 total contracts per market across ≤3 levels.
- [ ] Existing tests still pass: `uv run --all-extras pytest tests/test_strategies tests/test_main.py -v`.

## Documentation Requirement

- Task 04 captures the YAML delta in the CHANGELOG.
- Do not update README sizing examples in this task — those are illustrative and stale-tolerant.

## Commit Requirement

Separate commit on `feature/risk-caps`. Suggested message:

```
chore(strategies): size mentions for $90 account (5 contracts / 3 levels)

Worst-case per market: $4.25 (5 × $0.85 max NO bid). Stays well under
the $45 MAX_TOTAL_NOTIONAL_USD cap even with 8-10 markets matching.
```
