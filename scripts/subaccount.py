"""Kalshi Subaccount Management CLI.

Usage:
    python scripts/subaccount.py create
    python scripts/subaccount.py list
    python scripts/subaccount.py balance <N>
    python scripts/subaccount.py transfer <from> <to> <amount>
    python scripts/subaccount.py transfers [--limit N]
    python scripts/subaccount.py positions <N>
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config, ConfigError
from src.kalshi_client import KalshiClient, KalshiAPIError


def get_client() -> KalshiClient:
    """Initialize authenticated Kalshi client."""
    load_dotenv()
    config = load_config()

    with open(config.private_key_path, "r") as f:
        private_key_pem = f.read()

    return KalshiClient(
        api_key_id=config.api_key_id,
        private_key_pem=private_key_pem,
        base_url=config.api_base_url,
    )


def cmd_create(args) -> int:
    """Create a new subaccount."""
    client = get_client()

    try:
        result = client.post("/portfolio/subaccounts", {})
        subaccount_number = result.get("subaccount_number")

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Created subaccount #{subaccount_number}")

        return 0
    except KalshiAPIError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def cmd_list(args) -> int:
    """List all subaccounts with balances."""
    client = get_client()

    try:
        result = client.get("/portfolio/subaccounts/balances")
        balances = result.get("subaccount_balances", [])

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print()
            print("Subaccount Balances")
            print("=" * 40)
            for bal in balances:
                num = bal.get("subaccount_number", 0)
                balance = bal.get("balance", 0) / 100
                portfolio = bal.get("portfolio_value", 0) / 100

                label = "Primary (0)" if num == 0 else f"Sub {num}"
                print(f"  {label:12} ${balance:>10,.2f}  (portfolio: ${portfolio:,.2f})")
            print()

        return 0
    except KalshiAPIError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def cmd_balance(args) -> int:
    """Get balance for a specific subaccount."""
    client = get_client()
    subaccount = args.subaccount

    try:
        result = client.get("/portfolio/subaccounts/balances")
        balances = result.get("subaccount_balances", [])

        for bal in balances:
            if bal.get("subaccount_number") == subaccount:
                if args.json:
                    print(json.dumps(bal, indent=2))
                else:
                    balance = bal.get("balance", 0) / 100
                    portfolio = bal.get("portfolio_value", 0) / 100
                    label = "Primary" if subaccount == 0 else f"Subaccount {subaccount}"
                    print(f"{label}: ${balance:,.2f} (portfolio: ${portfolio:,.2f})")
                return 0

        print(f"Subaccount {subaccount} not found", file=sys.stderr)
        return 1
    except KalshiAPIError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def cmd_transfer(args) -> int:
    """Transfer funds between subaccounts."""
    client = get_client()

    # Convert amount from dollars to cents
    amount_cents = int(args.amount * 100)

    try:
        result = client.post("/portfolio/subaccounts/transfer", {
            "from_subaccount": args.from_account,
            "to_subaccount": args.to_account,
            "amount": amount_cents,
        })

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            from_label = "Primary" if args.from_account == 0 else f"Subaccount {args.from_account}"
            to_label = "Primary" if args.to_account == 0 else f"Subaccount {args.to_account}"
            print(f"Transferred ${args.amount:,.2f} from {from_label} to {to_label}")

        return 0
    except KalshiAPIError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def cmd_transfers(args) -> int:
    """View transfer history."""
    client = get_client()

    try:
        params = {"limit": args.limit}
        result = client.get("/portfolio/subaccounts/transfers", params=params)
        transfers = result.get("transfers", [])

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print()
            print("Recent Transfers")
            print("=" * 60)
            if not transfers:
                print("  No transfers found")
            else:
                for t in transfers:
                    from_acc = t.get("from_subaccount", 0)
                    to_acc = t.get("to_subaccount", 0)
                    amount = t.get("amount", 0) / 100
                    timestamp = t.get("created_time", "")[:19]

                    from_label = "Primary" if from_acc == 0 else f"Sub {from_acc}"
                    to_label = "Primary" if to_acc == 0 else f"Sub {to_acc}"
                    print(f"  {timestamp}  {from_label} -> {to_label}  ${amount:,.2f}")
            print()

        return 0
    except KalshiAPIError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def cmd_positions(args) -> int:
    """View positions for a subaccount."""
    client = get_client()

    try:
        result = client.get_positions(limit=100, count_filter="position")
        positions = result.get("market_positions", [])

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print()
            label = "Primary" if args.subaccount == 0 else f"Subaccount {args.subaccount}"
            print(f"Positions for {label}")
            print("=" * 60)
            if not positions:
                print("  No open positions")
            else:
                for pos in positions:
                    ticker = pos.get("ticker", "")
                    position = pos.get("position", 0)
                    if position != 0:
                        side = "Yes" if position > 0 else "No"
                        count = abs(position)
                        print(f"  {ticker:30} {count:>5} {side} contracts")
            print()

        return 0
    except KalshiAPIError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kalshi Subaccount Management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    subparsers.add_parser("create", help="Create a new subaccount")

    # list
    subparsers.add_parser("list", help="List all subaccounts with balances")

    # balance
    balance_parser = subparsers.add_parser("balance", help="Get balance for subaccount")
    balance_parser.add_argument("subaccount", type=int, help="Subaccount number (0=primary)")

    # transfer
    transfer_parser = subparsers.add_parser("transfer", help="Transfer funds between subaccounts")
    transfer_parser.add_argument("from_account", type=int, help="Source subaccount (0=primary)")
    transfer_parser.add_argument("to_account", type=int, help="Destination subaccount")
    transfer_parser.add_argument("amount", type=float, help="Amount in dollars")

    # transfers
    transfers_parser = subparsers.add_parser("transfers", help="View transfer history")
    transfers_parser.add_argument("--limit", type=int, default=20, help="Max results")

    # positions
    positions_parser = subparsers.add_parser("positions", help="View positions for subaccount")
    positions_parser.add_argument("subaccount", type=int, help="Subaccount number (0=primary)")

    args = parser.parse_args()

    commands = {
        "create": cmd_create,
        "list": cmd_list,
        "balance": cmd_balance,
        "transfer": cmd_transfer,
        "transfers": cmd_transfers,
        "positions": cmd_positions,
    }

    try:
        return commands[args.command](args)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
