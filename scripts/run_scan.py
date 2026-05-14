from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.pipeline.scanner_runner import ScannerRunConfig, ScannerRunner  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Phase 10 dry-run scanner pipeline.")
    parser.add_argument("--symbols", nargs="*", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--exchange", choices=["binance", "bybit"], default="binance")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--candle-limit", type=int, default=250)
    parser.add_argument("--account-equity", default="10000")
    parser.add_argument("--risk-per-trade-pct", default="1")
    parser.add_argument("--min-score-for-idea", default="80")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config = ScannerRunConfig(
        symbols=args.symbols,
        exchange=args.exchange,
        interval=args.interval,
        candle_limit=args.candle_limit,
        dry_run_alerts=True,
        account_equity=Decimal(args.account_equity),
        risk_per_trade_pct=Decimal(args.risk_per_trade_pct),
        min_score_for_idea=Decimal(args.min_score_for_idea),
    )

    result = await ScannerRunner().run(config)

    print("Phase 10 Scanner Runner")
    print(f"Exchange: {config.exchange}")
    print(f"Interval: {config.interval}")
    print(f"Symbols scanned: {result.scanned_symbols}")
    print(f"Trade ideas created: {result.trade_ideas_created}")
    print(f"Dry-run alerts created: {result.dry_run_alerts_created}")
    print(f"Journal entries created: {result.journal_entries_created}")
    print("")

    for symbol_result in result.results:
        detail = symbol_result.rejection_reason or symbol_result.error_message or "N/A"
        print(f"{symbol_result.symbol}: {symbol_result.status.value} | {detail}")


if __name__ == "__main__":
    asyncio.run(main())
