"""Command-line interface for the read-only sports scanner."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sports_arb import __version__
from sports_arb.clob import CLOB_BASE_URL, ClobClient
from sports_arb.detector import DetectorConfig, SameMarketDetector
from sports_arb.errors import SportsArbError
from sports_arb.gamma import GAMMA_BASE_URL, GammaClient
from sports_arb.http import JsonHttpClient
from sports_arb.scanner import ScanReport, SportsScanner, append_jsonl

LOGGER = logging.getLogger(__name__)


def decimal_arg(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("value must be finite")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sports-arb",
        description=(
            "Read-only Polymarket sports scanner. Prints theoretical complete-set "
            "indications after estimated costs. Never submits orders."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--market-types",
        nargs="+",
        default=["moneyline", "spreads", "totals"],
        help="Gamma sportsMarketType values (default: moneyline spreads totals)",
    )
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=1, help="0 means run until interrupted")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--discovery-refresh", type=float, default=300.0)
    parser.add_argument("--min-shares", type=decimal_arg, default=Decimal("5"))
    parser.add_argument("--max-shares", type=decimal_arg, default=Decimal("100"))
    parser.add_argument("--max-capital", type=decimal_arg, default=Decimal("100"))
    parser.add_argument("--min-net-edge-bps", type=decimal_arg, default=Decimal("20"))
    parser.add_argument("--min-net-profit", type=decimal_arg, default=Decimal("0.01"))
    parser.add_argument("--slippage-bps", type=decimal_arg, default=Decimal("10"))
    parser.add_argument("--settlement-cost-per-pair", type=decimal_arg, default=Decimal("0"))
    parser.add_argument("--max-book-age-ms", type=float, default=5000.0)
    parser.add_argument("--max-pair-skew-ms", type=float, default=1000.0)
    parser.add_argument(
        "--jsonl", type=Path, help="Append qualifying indications to this JSONL file"
    )
    parser.add_argument(
        "--jsonl-all",
        action="store_true",
        help="With --jsonl, also record evaluated below-threshold near misses",
    )
    parser.add_argument(
        "--show-all", action="store_true", help="Show all evaluated markets, not just top rows"
    )
    parser.add_argument("--top", type=int, default=10, help="Maximum default table rows")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    if args.iterations < 0:
        raise ValueError("iterations cannot be negative")
    if args.poll_interval < 0 or args.discovery_refresh <= 0:
        raise ValueError("invalid timing configuration")

    detector = SameMarketDetector(
        DetectorConfig(
            min_shares=args.min_shares,
            max_shares=args.max_shares,
            max_capital=args.max_capital,
            min_net_edge_bps=args.min_net_edge_bps,
            min_net_profit=args.min_net_profit,
            slippage_bps=args.slippage_bps,
            settlement_cost_per_pair=args.settlement_cost_per_pair,
            max_book_age_ms=args.max_book_age_ms,
            max_pair_skew_ms=args.max_pair_skew_ms,
        )
    )
    print(
        "READ-ONLY / NO ORDERS — indications are not executable-fill guarantees.",
        file=sys.stderr,
    )
    async with (
        JsonHttpClient(GAMMA_BASE_URL) as gamma_http,
        JsonHttpClient(CLOB_BASE_URL) as clob_http,
    ):
        scanner = SportsScanner(GammaClient(gamma_http), ClobClient(clob_http), detector)
        iteration = 0
        last_discovery = 0.0
        markets = []
        while args.iterations == 0 or iteration < args.iterations:
            started = time.monotonic()
            if not markets or started - last_discovery >= args.discovery_refresh:
                markets = await scanner.discover(tuple(args.market_types), args.max_markets)
                last_discovery = started
                LOGGER.info("Discovered %d orderable sports markets", len(markets))

            report = await scanner.scan_once(markets)
            _print_report(report, show_all=args.show_all, top=args.top)
            if args.jsonl:
                count = append_jsonl(args.jsonl, report, include_near_misses=args.jsonl_all)
                LOGGER.info("Appended %d JSONL records to %s", count, args.jsonl)

            iteration += 1
            if args.iterations == 0 or iteration < args.iterations:
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, args.poll_interval - elapsed))
    return 0


def _print_report(report: ScanReport, *, show_all: bool, top: int) -> None:
    ranked = sorted(
        (item for item in report.evaluations if item.opportunity is not None),
        key=lambda item: (
            item.status != "qualifying",
            -(item.opportunity.net_edge_bps if item.opportunity else Decimal(0)),
        ),
    )
    qualifying_count = len(report.qualifying)
    print(
        f"\nScan {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(report.scanned_at))} | "
        f"markets={report.discovered_markets} books={report.received_books} "
        f"qualifying={qualifying_count} statuses={report.status_counts}"
    )
    if not ranked:
        print("No markets had two valid, fresh ask ladders to evaluate.")
        return

    rows = ranked if show_all else ranked[: max(top, qualifying_count)]
    print("status      net_bps    shares      cost    net_pnl  gross_bps  age_ms  market")
    print("-" * 112)
    for evaluation in rows:
        opp = evaluation.opportunity
        if opp is None:
            continue
        question = opp.question.replace("\n", " ")[:48]
        print(
            f"{evaluation.status:<11} "
            f"{opp.net_edge_bps:>8.2f} "
            f"{opp.shares:>9.2f} "
            f"{opp.total_cost:>9.2f} "
            f"{opp.net_profit:>10.4f} "
            f"{opp.gross_top_edge_bps:>10.2f} "
            f"{opp.book_age_ms:>7.0f}  {question}"
        )
    if not show_all and len(ranked) > len(rows):
        print(f"... {len(ranked) - len(rows)} additional evaluated markets omitted")
    if qualifying_count == 0:
        print(
            "No qualifying indications. Near misses are shown for diagnostics; "
            "negative net_bps means estimated costs exceed the gross edge."
        )
    print(
        "Payout assumes one $1 winning share per pair; merge/redeem/resolution timing "
        "and two-leg fill risk are not modeled as execution guarantees."
    )


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("Stopped by user. No orders were sent.", file=sys.stderr)
        raise SystemExit(130) from None
    except (SportsArbError, ValueError) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
