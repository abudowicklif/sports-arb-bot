"""CLI for real authenticated Polymarket production trading."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_arb.clob import CLOB_BASE_URL, ClobClient
from sports_arb.data_api import DATA_API_BASE_URL, DataApiClient
from sports_arb.errors import SportsArbError
from sports_arb.gamma import GAMMA_BASE_URL, GammaClient
from sports_arb.http import JsonHttpClient
from sports_arb.live_config import (
    KILL_FILE,
    Credentials,
    LiveRiskConfig,
    assert_live_armed,
)
from sports_arb.live_execution import LiveExecutionError, LiveTrader
from sports_arb.live_gateway import PolymarketLiveGateway
from sports_arb.live_ledger import LiveLedger, redact
from sports_arb.paths import DATA_DIR
from sports_arb.user_stream import AuthenticatedUserStream

LOGGER = logging.getLogger(__name__)
LOCK_PATH = DATA_DIR / "live-process.lock"


def decimal_arg(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("invalid decimal") from exc
    if not result.is_finite():
        raise argparse.ArgumentTypeError("value must be finite")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sports-arb-live",
        description="REAL Polymarket production order execution. Funds are at risk.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("derive-credentials", help="Derive/create L2 API credentials with your signer")
    auth = sub.add_parser("auth-check", help="Authenticated read-only account check")
    auth.add_argument("--refresh-balance", action="store_true")

    trade = sub.add_parser("trade", help="Find and execute REAL sports complete-set orders")
    trade.add_argument("--confirm-live", required=True)
    trade.add_argument("--continuous", action="store_true")
    trade.add_argument("--poll-interval", type=float, default=2.0)
    trade.add_argument(
        "--market-types",
        nargs="+",
        default=["moneyline", "spreads", "totals", "futures", "props"],
    )
    trade.add_argument("--max-markets", type=int, default=25)
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

    sub.add_parser("kill", help="Activate kill file and request REAL cancel-all")
    reconcile = sub.add_parser("reconcile", help="Fetch real open orders, trades, and positions")
    reconcile.add_argument("--condition-id")
    return parser


async def run(args: argparse.Namespace) -> int:
    if args.command == "derive-credentials":
        await asyncio.to_thread(_derive_credentials)
        return 0

    credentials = Credentials.from_env()
    gateway = PolymarketLiveGateway(credentials)
    if args.command == "auth-check":
        ok, collateral, open_orders = await asyncio.gather(
            asyncio.to_thread(gateway.get_ok),
            asyncio.to_thread(gateway.collateral, refresh=args.refresh_balance),
            asyncio.to_thread(gateway.open_orders),
        )
        balance = Decimal(str(collateral.get("balance", "0"))) / Decimal("1000000")
        print(f"Production API response: {ok}")
        print(f"Signature type: {credentials.signature_type}")
        print(f"Funder/wallet: {gateway.account_address}")
        print(f"Available collateral reported by CLOB: {balance}")
        print(f"Allowance entries: {len(collateral.get('allowances') or {})}")
        print(f"Open orders: {len(open_orders)}")
        return 0

    if args.command == "kill":
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(f"{time.time()}: manual CLI kill\n", encoding="utf-8")
        result = await asyncio.to_thread(gateway.cancel_all)
        print(f"Kill switch active: {KILL_FILE}")
        print(json.dumps(redact(result), indent=2, default=str))
        return 0

    if args.command == "reconcile":
        async with JsonHttpClient(DATA_API_BASE_URL) as data_http:
            data_api = DataApiClient(data_http)
            open_orders = await asyncio.to_thread(gateway.open_orders, args.condition_id)
            trades = (
                await asyncio.to_thread(
                    gateway.market_trades, args.condition_id, first_page_only=True
                )
                if args.condition_id
                else []
            )
            positions = await data_api.positions(
                gateway.account_address, condition_id=args.condition_id
            )
        print(
            json.dumps(
                redact(
                    {
                        "open_orders": open_orders,
                        "recent_account_trades": trades,
                        "venue_positions": positions,
                    }
                ),
                indent=2,
                default=str,
            )
        )
        return 0

    if args.command == "trade":
        assert_live_armed(args.confirm_live)
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
                    trader = LiveTrader(
                        gamma=GammaClient(gamma_http),
                        clob=ClobClient(clob_http),
                        data_api=DataApiClient(data_http),
                        gateway=gateway,
                        user_stream=AuthenticatedUserStream(credentials),
                        ledger=ledger,
                        risk=risk,
                    )
                    while True:
                        result = await trader.trade_best(tuple(args.market_types), args.max_markets)
                        if result is not None:
                            _print_round_result(result)
                            if KILL_FILE.exists():
                                raise LiveExecutionError(
                                    f"round ended {result.status}; kill switch activated"
                                )
                        if not args.continuous:
                            return 0
                        await asyncio.sleep(args.poll_interval)
            finally:
                ledger.close()
    return 2


def _print_round_result(result: Any) -> None:
    print("\nREAL VENUE ROUND RESULT")
    print(f"round_id: {result.round_id}")
    print(f"status: {result.status}")
    print(f"market_id: {result.market_id}")
    print(f"condition_id: {result.condition_id}")
    print(f"order_ids: {', '.join(result.order_ids) or 'none'}")
    if result.estimated_realized_pnl is not None:
        print(f"estimated realized PnL after estimated fees: {result.estimated_realized_pnl}")
    else:
        print("realized PnL: not claimed while outcome positions remain open")
    if result.venue_positions:
        print("venue positions: " + json.dumps(redact(result.venue_positions), default=str))


def _derive_credentials() -> None:
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        raise SportsArbError("set POLYMARKET_PRIVATE_KEY before deriving credentials")
    from py_clob_client_v2 import ClobClient

    client = ClobClient("https://clob.polymarket.com", chain_id=137, key=private_key)
    creds = client.create_or_derive_api_key()
    # Deliberate user-requested credential output. Never write this to logs or DB.
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")


def _exclusive_live_lock():
    class LiveLock:
        def __enter__(self):
            LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.handle = LOCK_PATH.open("a+b")
            try:
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b"\0")
                    self.handle.flush()
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self.handle.close()
                raise LiveExecutionError(
                    "another live process holds the account lock"
                ) from exc
            return self

        def __exit__(self, exc_type, exc, tb):
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()

    return LiveLock()


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
        LOGGER.error("Interrupted. Run 'sports-arb-live kill' and reconcile open orders.")
        raise SystemExit(130) from None
    except (SportsArbError, ValueError) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
