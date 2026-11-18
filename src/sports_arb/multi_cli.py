"""Concurrent real-money Polymarket and Kalshi production coordinator."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from decimal import Decimal
from typing import Any

from sports_arb.clob import CLOB_BASE_URL, ClobClient
from sports_arb.data_api import DATA_API_BASE_URL, DataApiClient
from sports_arb.errors import SportsArbError
from sports_arb.gamma import GAMMA_BASE_URL, GammaClient
from sports_arb.http import JsonHttpClient
from sports_arb.kalshi_config import (
    KalshiCredentials,
    assert_kalshi_live_armed,
)
from sports_arb.kalshi_execution import KalshiTrader
from sports_arb.kalshi_gateway import KalshiGateway
from sports_arb.kalshi_stream import KalshiWebSocketStream
from sports_arb.live_cli import _exclusive_live_lock, decimal_arg
from sports_arb.live_config import (
    KILL_FILE,
    Credentials,
    LiveRiskConfig,
    assert_live_armed,
)
from sports_arb.live_execution import LiveExecutionError, LiveRoundResult, LiveTrader
from sports_arb.live_gateway import PolymarketLiveGateway
from sports_arb.live_ledger import LiveLedger, redact
from sports_arb.user_stream import AuthenticatedUserStream

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sports-arb-multi",
        description=(
            "Concurrent REAL Polymarket and Kalshi production execution. "
            "Each venue runs independent same-market strategies; funds are at risk."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth-check", help="Authenticated read-only checks on both production accounts")
    sub.add_parser("kill", help="Activate shared kill file and request REAL cancel-all on both")
    sub.add_parser("reconcile", help="Fetch real orders, fills, balances, and positions on both")
    trade = sub.add_parser("trade", help="Run both real production strategies concurrently")
    trade.add_argument("--confirm-live", required=True)
    trade.add_argument("--continuous", action="store_true")
    trade.add_argument("--poll-interval", type=float, default=2.0)
    trade.add_argument(
        "--market-types",
        nargs="+",
        default=["moneyline", "spreads", "totals", "futures", "props"],
    )
    trade.add_argument("--max-markets", type=int, default=25, help="Maximum markets per venue")
    trade.add_argument("--max-shares", type=decimal_arg, default=Decimal("5"))
    trade.add_argument("--max-pair-cost", type=decimal_arg, default=Decimal("10"))
    trade.add_argument("--max-daily-notional", type=decimal_arg, default=Decimal("20"))
    trade.add_argument("--max-in-flight-cost", type=decimal_arg, default=Decimal("20"))
    trade.add_argument("--max-daily-loss", type=decimal_arg, default=Decimal("1"))
    trade.add_argument("--max-unwind-loss", type=decimal_arg, default=Decimal("1"))
    trade.add_argument("--min-net-edge-bps", type=decimal_arg, default=Decimal("50"))
    trade.add_argument("--min-net-profit", type=decimal_arg, default=Decimal("0.01"))
    trade.add_argument("--order-timeout", type=float, default=12.0)
    trade.add_argument("--unwind-attempts", type=int, default=3)
    return parser


async def run(args: argparse.Namespace) -> int:
    # A manual kill must fail closed even when a credential file was lost or
    # misconfigured. Venue cancellation still requires the available credentials.
    if args.command == "kill":
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(f"{time.time()}: manual multi-venue CLI kill\n", encoding="utf-8")
    poly_credentials = Credentials.from_env()
    kalshi_credentials = KalshiCredentials.from_env()
    poly_gateway = PolymarketLiveGateway(poly_credentials)
    kalshi_gateway = KalshiGateway(kalshi_credentials)
    try:
        if args.command == "auth-check":
            return await _auth_check(poly_credentials, poly_gateway, kalshi_gateway)
        if args.command == "kill":
            return await _kill_both(poly_gateway, kalshi_gateway)
        if args.command == "reconcile":
            return await _reconcile(poly_gateway, kalshi_gateway)
        if args.command == "trade":
            assert_live_armed(args.confirm_live)
            assert_kalshi_live_armed(args.confirm_live)
            if args.poll_interval < 0:
                raise LiveExecutionError("poll interval cannot be negative")
            risk = LiveRiskConfig(
                max_pair_cost=args.max_pair_cost,
                max_daily_notional=args.max_daily_notional,
                max_in_flight_cost=args.max_in_flight_cost,
                max_daily_realized_loss=args.max_daily_loss,
                max_unwind_loss=args.max_unwind_loss,
                max_shares=args.max_shares,
                min_net_edge_bps=args.min_net_edge_bps,
                min_net_profit=args.min_net_profit,
                order_timeout_seconds=args.order_timeout,
                unwind_attempts=args.unwind_attempts,
            )
            risk.validate()
            with _exclusive_live_lock():
                ledger = LiveLedger()
                try:
                    async with (
                        JsonHttpClient(GAMMA_BASE_URL) as gamma_http,
                        JsonHttpClient(CLOB_BASE_URL) as clob_http,
                        JsonHttpClient(DATA_API_BASE_URL) as data_http,
                    ):
                        polymarket = LiveTrader(
                            gamma=GammaClient(gamma_http),
                            clob=ClobClient(clob_http),
                            data_api=DataApiClient(data_http),
                            gateway=poly_gateway,
                            user_stream=AuthenticatedUserStream(poly_credentials),
                            ledger=ledger,
                            risk=risk,
                        )
                        kalshi = KalshiTrader(
                            gateway=kalshi_gateway,
                            stream=KalshiWebSocketStream(kalshi_credentials),
                            ledger=ledger,
                            risk=risk,
                        )
                        await _run_concurrent(polymarket, kalshi, args)
                finally:
                    ledger.close()
            return 0
        return 2
    finally:
        await kalshi_gateway.close()


async def _run_concurrent(
    polymarket: LiveTrader, kalshi: KalshiTrader, args: argparse.Namespace
) -> None:
    async def worker(venue: str, trader: Any) -> None:
        while True:
            if KILL_FILE.exists():
                raise LiveExecutionError("shared kill switch is active")
            result = await trader.trade_best(tuple(args.market_types), args.max_markets)
            if result is not None:
                _print_result(venue, result)
                if KILL_FILE.exists():
                    raise LiveExecutionError(
                        f"{venue} round ended {result.status}; shared kill switch activated"
                    )
            if not args.continuous:
                return
            await asyncio.sleep(args.poll_interval)

    tasks = [
        asyncio.create_task(worker("polymarket", polymarket), name="polymarket-live-worker"),
        asyncio.create_task(worker("kalshi", kalshi), name="kalshi-live-worker"),
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(f"{time.time()}: concurrent worker failure/interruption\n")
        # Account cleanup is best-effort and must not hide the execution error.
        await asyncio.gather(
            asyncio.to_thread(polymarket.gateway.cancel_all),
            kalshi.gateway.cancel_all(),
            return_exceptions=True,
        )
        raise


async def _auth_check(
    credentials: Credentials,
    polymarket: PolymarketLiveGateway,
    kalshi: KalshiGateway,
) -> int:
    (
        poly_ok,
        collateral,
        poly_open,
        kalshi_status,
        kalshi_balance,
        kalshi_open,
    ) = await asyncio.gather(
        asyncio.to_thread(polymarket.get_ok),
        asyncio.to_thread(polymarket.collateral),
        asyncio.to_thread(polymarket.open_orders),
        kalshi.exchange_status(),
        kalshi.balance(),
        kalshi.open_orders(),
    )
    poly_balance = Decimal(str(collateral.get("balance", "0"))) / Decimal("1000000")
    print(
        json.dumps(
            redact(
                {
                    "polymarket": {
                        "production_response": poly_ok,
                        "signature_type": credentials.signature_type,
                        "account": polymarket.account_address,
                        "available_collateral": str(poly_balance),
                        "open_orders": len(poly_open),
                    },
                    "kalshi": {
                        "exchange_status": kalshi_status,
                        "available_balance": str(kalshi_balance),
                        "open_orders": len(kalshi_open),
                    },
                    "orders_submitted_by_auth_check": False,
                }
            ),
            indent=2,
            default=str,
        )
    )
    return 0


async def _kill_both(polymarket: PolymarketLiveGateway, kalshi: KalshiGateway) -> int:
    KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    KILL_FILE.write_text(f"{time.time()}: manual multi-venue CLI kill\n", encoding="utf-8")
    results = await asyncio.gather(
        asyncio.to_thread(polymarket.cancel_all), kalshi.cancel_all(), return_exceptions=True
    )
    print(f"Shared kill switch active: {KILL_FILE}")
    payload: dict[str, Any] = {}
    failed = False
    for venue, result in zip(("polymarket", "kalshi"), results, strict=True):
        if isinstance(result, BaseException):
            payload[venue] = {"cancel_error": str(result)}
            failed = True
        else:
            payload[venue] = result
    print(json.dumps(redact(payload), indent=2, default=str))
    if failed:
        raise LiveExecutionError("one or both venue cancellations failed; reconcile immediately")
    return 0


async def _reconcile(polymarket: PolymarketLiveGateway, kalshi: KalshiGateway) -> int:
    async with JsonHttpClient(DATA_API_BASE_URL) as data_http:
        data_api = DataApiClient(data_http)
        (
            poly_open,
            poly_positions,
            kalshi_open,
            kalshi_positions,
            kalshi_fills,
            kalshi_balance,
        ) = await asyncio.gather(
            asyncio.to_thread(polymarket.open_orders),
            data_api.positions(polymarket.account_address),
            kalshi.open_orders(),
            kalshi.positions(),
            kalshi.recent_fills(),
            kalshi.balance(),
        )
    print(
        json.dumps(
            redact(
                {
                    "polymarket": {
                        "open_orders": poly_open,
                        "venue_positions": poly_positions,
                    },
                    "kalshi": {
                        "open_orders": kalshi_open,
                        "recent_fills": kalshi_fills,
                        "venue_positions": kalshi_positions,
                        "available_balance": str(kalshi_balance),
                    },
                }
            ),
            indent=2,
            default=str,
        )
    )
    return 0


def _print_result(venue: str, result: LiveRoundResult) -> None:
    print(f"\nREAL {venue.upper()} ROUND RESULT")
    print(f"round_id: {result.round_id}")
    print(f"status: {result.status}")
    print(f"market_id: {result.market_id}")
    print(f"order_ids: {', '.join(result.order_ids) or 'none'}")
    if result.estimated_realized_pnl is not None:
        label = "venue-reported" if venue == "kalshi" else "estimated after estimated fees"
        print(f"realized PnL ({label}): {result.estimated_realized_pnl}")
    else:
        print("realized PnL: not claimed without complete flat-position evidence")
    if result.venue_positions:
        print("venue positions: " + json.dumps(redact(result.venue_positions), default=str))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = build_parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        LOGGER.error("Interrupted. Run 'sports-arb-multi kill' and reconcile both accounts.")
        raise SystemExit(130) from None
    except (SportsArbError, ValueError) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
