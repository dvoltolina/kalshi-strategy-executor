#!/usr/bin/env python3
"""Generate a verifiable PnL report for bot trades from the Kalshi API.

Pulls fills, settlements, and balance from Kalshi's exchange records.
Filters to bot-only trades using order IDs from the local database,
then computes PnL entirely from API data (fill prices, quantities,
and settlement outcomes).

The raw API responses are saved alongside the report for full auditability.
Anyone can verify by checking order IDs against GET /portfolio/orders/{id}.

Usage:
    uv run python scripts/pnl_report.py
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.kalshi_client import KalshiClient


def create_client() -> KalshiClient:
    load_dotenv()
    key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    with open(key_path) as f:
        private_key_pem = f.read()
    return KalshiClient(
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_pem=private_key_pem,
        base_url=os.environ.get(
            "KALSHI_API_BASE_URL",
            "https://api.elections.kalshi.com/trade-api/v2",
        ),
    )


def get_bot_order_ids() -> set:
    """Get all Kalshi order IDs placed by the bot from the local database."""
    db_path = os.environ.get("DATABASE_PATH", "data/orders.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT order_id FROM orders "
        "WHERE order_id IS NOT NULL AND status != 'dry_run'"
    )
    order_ids = {r[0] for r in cur.fetchall()}
    conn.close()
    return order_ids


def fetch_all(client: KalshiClient, endpoint: str, key: str) -> list:
    """Paginate through all records from a Kalshi API endpoint."""
    all_records = []
    cursor = None
    page = 0
    while True:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        result = client.get(endpoint, params=params)
        records = result.get(key, [])
        all_records.extend(records)
        page += 1
        print(f"  Page {page}: {len(records)} records (total: {len(all_records)})")
        cursor = result.get("cursor")
        if not cursor or not records:
            break
    return all_records


def compute_bot_pnl(fills: list, settlements: list, bot_order_ids: set) -> dict:
    """Compute PnL for bot trades only.

    1. Filter fills to bot order IDs
    2. Group bot fills by ticker to get per-market cost and quantity
    3. Join with settlement results to determine win/loss
    4. Compute PnL per market: win = (count * $1) - cost, loss = -cost
    """
    # Filter to bot fills
    bot_fills = [f for f in fills if f["order_id"] in bot_order_ids]

    # Group fills by ticker
    # Each fill: side, count_fp, no_price_dollars/yes_price_dollars, fee_cost
    positions = {}  # ticker -> {side, count, cost, fees}
    for f in bot_fills:
        ticker = f["ticker"]
        count = float(f.get("count_fp", "0"))
        side = f.get("side", "")
        if side == "no":
            price = float(f.get("no_price_dollars", "0"))
        else:
            price = float(f.get("yes_price_dollars", "0"))
        fee = float(f.get("fee_cost", "0"))

        if ticker not in positions:
            positions[ticker] = {"side": side, "count": 0.0, "cost": 0.0, "fees": 0.0}
        positions[ticker]["count"] += count
        positions[ticker]["cost"] += price * count
        positions[ticker]["fees"] += fee

    # Build settlement lookup: ticker -> market_result
    settlement_lookup = {s["ticker"]: s["market_result"] for s in settlements}

    # Compute PnL per market
    total_cost = 0.0
    total_revenue = 0.0
    total_fees = 0.0
    wins = 0
    losses = 0
    breakeven = 0
    settled_count = 0
    unsettled_tickers = []
    per_market = []

    for ticker, pos in sorted(positions.items()):
        result = settlement_lookup.get(ticker)
        if result is None:
            unsettled_tickers.append(ticker)
            continue

        settled_count += 1
        cost = pos["cost"]
        count = pos["count"]
        side = pos["side"]
        total_cost += cost
        total_fees += pos["fees"]

        if side == result:
            # Win: payout is $1 per contract
            revenue = count
            pnl = revenue - cost
        else:
            # Loss: no payout
            revenue = 0.0
            pnl = -cost

        total_revenue += revenue

        if pnl > 0.001:
            wins += 1
        elif pnl < -0.001:
            losses += 1
        else:
            breakeven += 1

        per_market.append({
            "ticker": ticker,
            "side": side,
            "count": count,
            "cost": cost,
            "revenue": revenue,
            "pnl": pnl,
            "result": result,
        })

    total_pnl = total_revenue - total_cost
    traded = wins + losses + breakeven

    return {
        "total_fills": len(bot_fills),
        "total_markets": len(positions),
        "settled_markets": settled_count,
        "unsettled_markets": len(unsettled_tickers),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": wins / traded * 100 if traded else 0,
        "total_cost": total_cost,
        "total_revenue": total_revenue,
        "total_fees": total_fees,
        "total_pnl": total_pnl,
        "roi": total_pnl / total_cost * 100 if total_cost else 0,
        "per_market": per_market,
        "unsettled_tickers": unsettled_tickers,
    }


def generate_report(pnl: dict, balance: dict, timestamp: str) -> str:
    """Generate human-readable report text."""
    lines = []
    lines.append("=" * 60)
    lines.append("KALSHI BOT PnL REPORT")
    lines.append("=" * 60)
    lines.append(f"Generated:  {timestamp}")
    lines.append(f"Source:     Kalshi API (/portfolio/fills, /portfolio/settlements)")
    lines.append(f"Method:     Fills filtered to bot order IDs, PnL computed from")
    lines.append(f"            API fill prices and settlement outcomes.")
    lines.append(f"            Raw API data saved for independent verification.")
    lines.append("")

    lines.append("ACCOUNT BALANCE")
    lines.append("-" * 40)
    bal = balance.get("balance", 0) / 100
    portfolio = balance.get("portfolio_value", 0) / 100
    lines.append(f"  Cash balance:     ${bal:>12,.2f}")
    lines.append(f"  Portfolio value:  ${portfolio:>12,.2f}")
    lines.append(f"  Total equity:     ${bal + portfolio:>12,.2f}")
    lines.append("")

    lines.append("BOT PERFORMANCE (settled markets)")
    lines.append("-" * 40)
    lines.append(f"  Fills:              {pnl['total_fills']:>8,}")
    lines.append(f"  Markets traded:     {pnl['total_markets']:>8,}")
    lines.append(f"  Markets settled:    {pnl['settled_markets']:>8,}")
    lines.append(f"  Markets pending:    {pnl['unsettled_markets']:>8,}")
    lines.append(f"  Wins:               {pnl['wins']:>8,}")
    lines.append(f"  Losses:             {pnl['losses']:>8,}")
    lines.append(f"  Win rate:           {pnl['win_rate']:>7.1f}%")
    lines.append("")

    lines.append("FINANCIAL SUMMARY")
    lines.append("-" * 40)
    lines.append(f"  Total cost:       ${pnl['total_cost']:>12,.2f}")
    lines.append(f"  Total revenue:    ${pnl['total_revenue']:>12,.2f}")
    lines.append(f"  Total fees:       ${pnl['total_fees']:>12,.2f}")
    lines.append(f"  Net P&L:          ${pnl['total_pnl']:>+12,.2f}")
    lines.append(f"  ROI:              {pnl['roi']:>+11.1f}%")
    lines.append("")

    # Top wins and losses
    per_market = pnl["per_market"]
    by_pnl = sorted(per_market, key=lambda x: x["pnl"], reverse=True)
    top_wins = [m for m in by_pnl if m["pnl"] > 0][:10]
    top_losses = [m for m in by_pnl if m["pnl"] < 0][-10:]
    top_losses.reverse()

    if top_wins:
        lines.append("TOP WINS")
        lines.append("-" * 40)
        for m in top_wins:
            lines.append(
                f"  {m['ticker']:<40} "
                f"{m['count']:>6.0f} {m['side'].upper():>3} "
                f"${m['pnl']:>+9,.2f}"
            )
        lines.append("")

    if top_losses:
        lines.append("TOP LOSSES")
        lines.append("-" * 40)
        for m in top_losses:
            lines.append(
                f"  {m['ticker']:<40} "
                f"{m['count']:>6.0f} {m['side'].upper():>3} "
                f"${m['pnl']:>+9,.2f}"
            )
        lines.append("")

    lines.append("VERIFICATION")
    lines.append("-" * 40)
    lines.append("  Raw API responses saved alongside this report:")
    lines.append("    - fills.json            (all account fills from API)")
    lines.append("    - settlements.json      (all settlement outcomes from API)")
    lines.append("    - bot_fills.json        (fills filtered to bot order IDs)")
    lines.append("    - bot_order_ids.json    (order IDs used for filtering)")
    lines.append("    - balance.json          (account balance snapshot)")
    lines.append("    - pnl_report.py         (this script)")
    lines.append("")
    lines.append("  Verify any order: GET /portfolio/orders/{order_id}")
    lines.append("  Reproduce: uv run python scripts/pnl_report.py")
    lines.append("=" * 60)

    return "\n".join(lines)


def main():
    client = create_client()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    report_dir = Path("pnl_reports") / timestamp
    report_dir.mkdir(parents=True, exist_ok=True)

    print(f"Report directory: {report_dir}")
    print()

    # Load bot order IDs from local DB
    print("Loading bot order IDs from database...")
    bot_order_ids = get_bot_order_ids()
    print(f"  {len(bot_order_ids)} order IDs")
    print()

    # Fetch balance
    print("Fetching account balance...")
    balance = client.get_balance()
    print(f"  Balance: ${balance.get('balance', 0)/100:,.2f}")
    print()

    # Fetch all settlements
    print("Fetching settlements from Kalshi API...")
    settlements = fetch_all(client, "/portfolio/settlements", "settlements")
    print(f"  Total: {len(settlements)}")
    print()

    # Fetch all fills
    print("Fetching fills from Kalshi API...")
    fills = fetch_all(client, "/portfolio/fills", "fills")
    print(f"  Total: {len(fills)}")
    print()

    # Compute bot-only PnL
    print("Computing bot PnL from exchange records...")
    bot_fills = [f for f in fills if f["order_id"] in bot_order_ids]
    pnl = compute_bot_pnl(fills, settlements, bot_order_ids)
    print(f"  {pnl['total_fills']} bot fills across {pnl['total_markets']} markets")
    print()

    # Generate report
    report_text = generate_report(pnl, balance, timestamp)
    print(report_text)

    # Save everything
    print()
    print(f"Saving to {report_dir}/...")

    with open(report_dir / "settlements.json", "w") as f:
        json.dump(settlements, f, indent=2, default=str)

    with open(report_dir / "fills.json", "w") as f:
        json.dump(fills, f, indent=2, default=str)

    with open(report_dir / "bot_fills.json", "w") as f:
        json.dump(bot_fills, f, indent=2, default=str)

    with open(report_dir / "bot_order_ids.json", "w") as f:
        json.dump(sorted(bot_order_ids), f, indent=2)

    with open(report_dir / "balance.json", "w") as f:
        json.dump(balance, f, indent=2, default=str)

    with open(report_dir / "report.txt", "w") as f:
        f.write(report_text)

    # Save PnL summary with per-market detail
    summary = {
        k: v for k, v in pnl.items() if k != "per_market"
    }
    with open(report_dir / "pnl_summary.json", "w") as f:
        json.dump(
            {"pnl": summary, "balance": balance, "generated": timestamp},
            f,
            indent=2,
            default=str,
        )

    with open(report_dir / "per_market_pnl.json", "w") as f:
        json.dump(pnl["per_market"], f, indent=2, default=str)

    # Copy this script for reproducibility
    shutil.copy2(__file__, report_dir / "pnl_report.py")

    print(f"  settlements.json     ({len(settlements)} records)")
    print(f"  fills.json           ({len(fills)} records)")
    print(f"  bot_fills.json       ({len(bot_fills)} records)")
    print(f"  bot_order_ids.json   ({len(bot_order_ids)} IDs)")
    print(f"  balance.json")
    print(f"  report.txt")
    print(f"  pnl_summary.json")
    print(f"  per_market_pnl.json  ({len(pnl['per_market'])} markets)")
    print(f"  pnl_report.py")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
