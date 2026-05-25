# Prompt Pack: Risk Caps for Live $90 Deployment

Workstream goal: introduce a global account-level dollar exposure cap, lower the default mentions strategy sizing for a $90 funded account, and fix the local macOS Python SSL trust store so the runner's WebSocket connects cleanly. Result is a `feature/risk-caps` branch reviewable before any flip of `DRY_RUN=false`.

## Tasks

| File | Summary | Complexity | Dependencies | Target files | Verify | Parallel-safe? |
|---|---|---|---|---|---|---|
| `01_account_budget.md` | Add `OrderBudget` + wire global $ cap through Config → OrderPlacer → Runner & one-shot main | Large | None | `src/config.py`, `src/order_budget.py` (new), `src/order_placer.py`, `src/runner.py`, `src/main.py`, `.env.example`, `tests/test_order_budget.py` (new), `tests/test_order_placer.py` | `uv run --all-extras pytest tests/ -v` | No (touches multiple core modules in one diff) |
| `02_mentions_sizing.md` | Lower `mentions.yaml` to 5 contracts / 3 levels for $90 account | Small | None | `strategies/mentions.yaml` | `uv run python -m src.main --strategy mentions --dry-run` (inspect ladder output) | Yes — disjoint from 01 |
| `03_macos_ssl_fix.md` | Run macOS Python 3.14 cert installer; verify WS connects | Small | None (environment-only) | None (host-side action) | `uv run python -m src.main --run --dry-run` (no `CERTIFICATE_VERIFY_FAILED` lines) | Yes — disjoint |
| `04_changelog_and_env_example.md` | CHANGELOG entry + `.env.example` documents `MAX_TOTAL_NOTIONAL_USD` | Small | 01 (cites the new env var) | `CHANGELOG_RISK_CAPS_2026_05_25.md` (new), `.env.example` | Visual diff | No — depends on 01 |

## Execution Order

Serial when dependencies require it; parallel where disjoint.

1. **In parallel**: `01_account_budget.md`, `02_mentions_sizing.md`, `03_macos_ssl_fix.md`.
   - 01 and 02 do not share files. 03 is an environment action with no repo write.
2. **After 01 lands**: `04_changelog_and_env_example.md` (needs the exact env var name and behavior locked in).

## Known Risks

- **Order site duplication**: There are currently two order-placement paths (`main.py::run_strategy` for one-shot and `runner.py::_run_single_strategy` for continuous). 01 must wire the budget through both, or live mode will silently bypass the cap. Acceptance criteria spell this out.
- **Default cap behavior**: When `DRY_RUN=true`, missing `MAX_TOTAL_NOTIONAL_USD` defaults to a permissive value so dry-runs keep working. When `DRY_RUN=false`, missing cap must raise `ConfigError` at startup. Easy to get backwards; tests must cover both.
- **Mid-tick budget exhaustion**: Behavior should be "reject remaining orders with a clear log line", not "raise". A raise inside the tick would crash the runner.
- **Pre-existing positions**: If the account already has $ outstanding above the cap, no new orders are placed. Print a startup warning, do not crash.
- **macOS cert installer**: Touches the system Python install, not the venv. The change is global to the user's Framework Python. The installer ships with Python itself and is the canonical fix; alternatives (manually setting `SSL_CERT_FILE`) are equally valid but messier.

## Final Verification Checklist (after all tasks land)

- [ ] `uv run --all-extras pytest tests/ -v` — all green.
- [ ] `uv run python -m src.main --list-strategies` — runs.
- [ ] `uv run python -m src.main --strategy mentions --dry-run` — ladder shows ≤5 contracts across ≤3 levels per market; budget log line visible.
- [ ] `uv run python -m src.main --run --dry-run` — no `CERTIFICATE_VERIFY_FAILED` lines; tick loop enters and reports a budget summary; clean Ctrl+C shutdown.
- [ ] `CHANGELOG_RISK_CAPS_2026_05_25.md` exists at repo root and describes deltas + worst-case math.
- [ ] `.env.example` lists `MAX_TOTAL_NOTIONAL_USD` with a comment explaining when it's required.
- [ ] Branch `feature/risk-caps` pushed; `main` untouched; PR draftable.
- [ ] **Human has reviewed the diff and explicitly authorized `DRY_RUN=false` before any live action.** Agents must not flip it.
