# 04 — CHANGELOG + `.env.example` Documentation

## Task

Write `CHANGELOG_RISK_CAPS_2026_05_25.md` and ensure `.env.example` documents the new `MAX_TOTAL_NOTIONAL_USD` variable.

## Context

Per `AGENTS.md` documentation requirements, any change to risk controls, default sizing, or required env vars needs a dated CHANGELOG. Task 01 adds an env var. Task 02 changes default sizing. Task 03 is a host-side fix. All three need to be captured in one document for this date.

## Required Reading

- `AGENTS.md` "Documentation Requirements" section.
- The final state of `src/config.py` after task 01 lands (for exact env var name + behavior).
- The final state of `strategies/mentions.yaml` after task 02 lands.
- Git log on `feature/risk-caps` for commit messages to cite.

## Scope Boundaries

May create:

- `CHANGELOG_RISK_CAPS_2026_05_25.md`

May modify:

- `.env.example` — only to ensure `MAX_TOTAL_NOTIONAL_USD` is documented (task 01 should have done this, but verify and tidy).

Off-limits:

- Anything else.

## Dependencies

Depends on `01_account_budget.md` (env var name and behavior must be finalized).

## Implementation Notes

CHANGELOG structure (follow pepSmart pattern):

```markdown
# CHANGELOG — Risk Caps — 2026-05-25

## Summary
One paragraph: why these changes exist (sizing the bot for a $90 funded account before flipping `DRY_RUN=false`).

## Changes

### Added
- `MAX_TOTAL_NOTIONAL_USD` env var ...
- `src/order_budget.py` ...
- `tests/test_order_budget.py` ...
- `AGENTS.md`, `ARCHITECTURE.md` ...

### Changed
- `strategies/mentions.yaml`: contracts_per_market 500 → 5, levels 7 → 3.
- `src/order_placer.py`: accepts optional OrderBudget; pre-flight reservation.
- `src/runner.py`: builds OrderBudget per trade tick.
- `src/main.py::run_strategy`: builds OrderBudget for one-shot path.
- `.env.example`: documents MAX_TOTAL_NOTIONAL_USD.

### Fixed
- macOS Python 3.14 SSL trust store on the deployment host (no repo change).

## Risk Math
- Cap: $45 outstanding.
- Mentions sizing: 5 × $0.85 = $4.25 worst-case per market.
- Capacity under cap: ~10 mentions markets max in flight simultaneously.

## Verification
List commands run + outcomes.

## Audit Findings
Cite each audit agent + their findings + how fixed.

## Known Limitations
- No per-strategy budget split.
- No daily loss limit.
- No dynamic cap-as-percent-of-balance.
- Cap refresh is per-tick (5 min cadence); intra-tick partial fills don't refund budget until next tick.

## Next Steps
- Operator review of diff on `feature/risk-caps`.
- Operator flips `DRY_RUN=false` only after review.
- Recommended first live action: place ONE single contract on a known market via `--strategy mentions --confirm`, verify fill, before letting `--run` go unsupervised.
```

`.env.example`: verify task 01's addition reads cleanly. The example value should be `45` (matches the operator's chosen cap).

## Acceptance Criteria

- [ ] `CHANGELOG_RISK_CAPS_2026_05_25.md` exists at repo root.
- [ ] CHANGELOG has Summary, Changes (Added/Changed/Fixed), Risk Math, Verification, Audit Findings, Known Limitations, Next Steps sections.
- [ ] `.env.example` contains `MAX_TOTAL_NOTIONAL_USD=45` and a comment block explaining when it's required.
- [ ] No source files modified.

## Documentation Requirement

This task is the documentation.

## Commit Requirement

Single commit on `feature/risk-caps`. Suggested message:

```
docs(risk): CHANGELOG and .env.example for risk-caps workstream
```
