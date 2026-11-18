"""Real two-leg production execution, reconciliation, and failed-hedge unwinds."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from sports_arb.book import OrderBook, OrderBookCache
from sports_arb.clob import ClobClient
from sports_arb.data_api import DataApiClient
from sports_arb.detector import Opportunity, SameMarketDetector
from sports_arb.errors import SportsArbError
from sports_arb.gamma import GammaClient
from sports_arb.live_config import KILL_FILE, LiveRiskConfig
from sports_arb.live_gateway import PolymarketLiveGateway
from sports_arb.live_ledger import LiveLedger
from sports_arb.live_models import (
    OrderState,
    OrderSubmission,
    VenueFill,
    fills_for_orders,
)
from sports_arb.models import ONE, ZERO, SportsMarket
from sports_arb.scanner import SportsScanner
from sports_arb.strategy import StrategyConfig, cap_plan, detector_config
from sports_arb.user_stream import AuthenticatedUserStream

LOGGER = logging.getLogger(__name__)


class LiveExecutionError(SportsArbError):
    pass


@dataclass(frozen=True, slots=True)
class LiveRoundResult:
    round_id: str
    status: str
    market_id: str
    condition_id: str
    order_ids: tuple[str, ...]
    estimated_realized_pnl: Decimal | None
    venue_positions: list[dict[str, Any]]


class LiveTrader:
    def __init__(
        self,
        *,
        gamma: GammaClient,
        clob: ClobClient,
        data_api: DataApiClient,
        gateway: PolymarketLiveGateway,
        user_stream: AuthenticatedUserStream,
        ledger: LiveLedger,
        risk: LiveRiskConfig,
    ) -> None:
        risk.validate()
        self.gamma = gamma
        self.clob = clob
        self.data_api = data_api
        self.gateway = gateway
        self.user_stream = user_stream
        self.ledger = ledger
        self.risk = risk
        self.strategy = StrategyConfig.load()
        self.detector = SameMarketDetector(detector_config(risk, self.strategy, venue="polymarket"))
        self.scanner = SportsScanner(gamma, clob, self.detector)

    async def trade_best(
        self, market_types: tuple[str, ...], max_markets: int
    ) -> LiveRoundResult | None:
        """Find and genuinely execute one qualifying pair, or return None."""
        open_orders = await asyncio.to_thread(self.gateway.open_orders)
        if open_orders:
            raise LiveExecutionError(
                f"refusing to run with {len(open_orders)} existing open orders; "
                "reconcile or cancel them first"
            )
        markets = await self.scanner.discover(market_types, max_markets)
        report = await self.scanner.scan_once(markets)
        candidates = [
            evaluation.opportunity
            for evaluation in report.qualifying
            if evaluation.opportunity is not None
        ]
        if not candidates:
            LOGGER.info(
                "No live candidate survived costs: markets=%d statuses=%s",
                len(markets),
                report.status_counts,
            )
            return None
        opportunity = max(candidates, key=lambda item: item.net_profit)
        market = next(item for item in markets if item.market_id == opportunity.market_id)
        return await self.execute(market, opportunity)

    async def execute(self, market: SportsMarket, prior: Opportunity) -> LiveRoundResult:
        """Execute using a fresh pair snapshot, never a stale scanner indication."""
        fresh_books = await self.clob.get_books(market.asset_ids)
        cache = OrderBookCache()
        for book in fresh_books.values():
            cache.put(book)
        evaluation = self.detector.evaluate(market, cache)
        if evaluation.status != "qualifying" or evaluation.opportunity is None:
            raise LiveExecutionError(
                f"opportunity disappeared on fresh validation: {evaluation.status}"
            )
        opp = evaluation.opportunity
        book_0 = cache.get(market.asset_ids[0])
        book_1 = cache.get(market.asset_ids[1])
        if book_0 is None or book_1 is None:
            raise LiveExecutionError("fresh validation lost an order book")

        limit_0, limit_1, cap_cost, cap_edge_bps = cap_plan(market, opp.shares, book_0, book_1)
        if cap_cost > self.risk.max_pair_cost:
            raise LiveExecutionError("price-capped pair cost exceeds live capital limit")
        if cap_edge_bps < self.risk.min_net_edge_bps:
            raise LiveExecutionError("edge does not survive protected limit prices")

        daily_committed = self.ledger.daily_committed_cost()
        if daily_committed + cap_cost > self.risk.max_daily_notional:
            raise LiveExecutionError("daily committed-notional limit would be exceeded")
        daily_pnl = self.ledger.daily_estimated_realized_pnl()
        if daily_pnl <= -self.risk.max_daily_realized_loss:
            raise LiveExecutionError("daily estimated realized-loss limit reached")
        balance = await asyncio.to_thread(self.gateway.collateral_balance)
        if balance < cap_cost:
            raise LiveExecutionError(
                f"insufficient available collateral: need {cap_cost}, have {balance}"
            )

        round_id = self.ledger.create_round(
            market.market_id,
            market.condition_id,
            opp.shares,
            cap_cost,
            venue="polymarket",
            max_daily_notional=self.risk.max_daily_notional,
            max_in_flight_cost=self.risk.max_in_flight_cost,
            max_daily_realized_loss=self.risk.max_daily_realized_loss,
        )
        self.ledger.event(
            round_id,
            "LIVE_INTENT",
            {
                "market": market.question,
                "shares": str(opp.shares),
                "limits": [str(limit_0), str(limit_1)],
                "cap_cost": str(cap_cost),
                "cap_edge_bps": str(cap_edge_bps),
                "prior_book_age_ms": prior.book_age_ms,
                "fresh_book_age_ms": opp.book_age_ms,
            },
        )
        heartbeat = _Heartbeat(self.gateway)
        order_states: tuple[OrderState, OrderState] | None = None
        unwind_states: list[OrderState] = []
        try:
            await self.user_stream.start()
            if not self.user_stream.healthy:
                raise LiveExecutionError("authenticated user stream is not healthy")
            await heartbeat.start()
            signed_0 = await asyncio.to_thread(
                self.gateway.sign_order,
                market,
                asset_id=market.asset_ids[0],
                side="BUY",
                price=limit_0,
                size=opp.shares,
                tick_size=book_0.tick_size or Decimal("0.01"),
            )
            signed_1 = await asyncio.to_thread(
                self.gateway.sign_order,
                market,
                asset_id=market.asset_ids[1],
                side="BUY",
                price=limit_1,
                size=opp.shares,
                tick_size=book_1.tick_size or Decimal("0.01"),
            )
            if not self.user_stream.healthy or not heartbeat.healthy:
                raise LiveExecutionError("health check failed immediately before submission")

            LOGGER.warning(
                "SUBMITTING REAL FOK PAIR market=%s shares=%s max_cost=%s",
                market.market_id,
                opp.shares,
                cap_cost,
            )
            try:
                raw_submissions = await asyncio.to_thread(
                    self.gateway.post_pair_fok, (signed_0, signed_1)
                )
            except Exception as exc:
                # A network error can occur after venue acceptance. Never retry
                # the signed batch blindly. Best-effort cancel and reconcile.
                self.ledger.event(round_id, "SUBMISSION_UNKNOWN", str(exc))
                await self._best_effort_market_cancel(round_id, market.condition_id)
                raise LiveExecutionError(
                    "pair submission outcome is unknown; market cancel requested; "
                    "manual reconciliation required"
                ) from exc

            submissions = tuple(OrderSubmission.from_raw(raw) for raw in raw_submissions)
            for index, submission in enumerate(submissions):
                self.ledger.record_submission(
                    round_id,
                    submission,
                    asset_id=market.asset_ids[index],
                    side="BUY",
                    price=(limit_0, limit_1)[index],
                    size=opp.shares,
                )
            order_states = await asyncio.gather(
                self._monitor_order(round_id, submissions[0], opp.shares, market.seconds_delay),
                self._monitor_order(round_id, submissions[1], opp.shares, market.seconds_delay),
            )
            trades = await self._trades_for_states(market.condition_id, order_states)
            self.ledger.record_fills(trades)

            filled_0, filled_1 = order_states[0].size_matched, order_states[1].size_matched
            if filled_0 != filled_1:
                exposure = abs(filled_0 - filled_1)
                LOGGER.error(
                    "REAL UNHEDGED EXPOSURE market=%s shares=%s; starting capped unwind",
                    market.market_id,
                    exposure,
                )
                unwind_states = await self._unwind_excess(
                    round_id,
                    market,
                    order_states,
                    trades,
                )
                trades = await self._trades_for_states(
                    market.condition_id, (*order_states, *unwind_states)
                )
                self.ledger.record_fills(trades)

            return await self._finish_round(round_id, market, order_states, unwind_states, trades)
        except asyncio.CancelledError:
            self.ledger.event(round_id, "EXECUTION_CANCELLED", "process cancellation")
            self._activate_kill_switch("process cancelled during live execution")
            await asyncio.shield(self._best_effort_market_cancel(round_id, market.condition_id))
            raise
        except Exception as exc:
            self.ledger.event(round_id, "EXECUTION_FAILURE", str(exc))
            self._activate_kill_switch(f"execution failure: {exc}")
            await self._best_effort_market_cancel(round_id, market.condition_id)
            raise
        finally:
            for event in self.user_stream.drain():
                self.ledger.event(round_id, "USER_WS", event)
            await heartbeat.stop()
            await self.user_stream.stop()

    async def _monitor_order(
        self,
        round_id: str,
        submission: OrderSubmission,
        expected_size: Decimal,
        seconds_delay: int,
    ) -> OrderState:
        if not submission.accepted:
            return OrderState.rejected(submission)
        immediate = OrderState.from_unmatched_submission(submission)
        if immediate is not None:
            return immediate
        assert submission.order_id is not None
        order_id = submission.order_id
        timeout = self.risk.order_timeout_seconds + max(0, seconds_delay)
        deadline = time.monotonic() + timeout
        cancellation_requested = False
        last_error: str | None = None
        while True:
            for event in self.user_stream.drain():
                self.ledger.event(round_id, "USER_WS", event)
            try:
                raw = await asyncio.to_thread(self.gateway.get_order, order_id)
                state = OrderState.from_raw(raw, order_id)
                self.ledger.record_state(round_id, state)
                # MATCHED with zero size can be a transient indexing state. Do
                # not turn it into a false no-fill conclusion.
                if state.terminal and not (
                    state.status == "MATCHED"
                    and expected_size > ZERO
                    and state.size_matched == ZERO
                ):
                    return state
            except Exception as exc:
                last_error = str(exc)
                self.ledger.event(round_id, "ORDER_QUERY_ERROR", last_error)

            if time.monotonic() >= deadline:
                if not cancellation_requested:
                    cancellation_requested = True
                    deadline = time.monotonic() + 5
                    self.ledger.event(round_id, "CANCEL_REQUESTED", order_id)
                    try:
                        await asyncio.to_thread(self.gateway.cancel_order, order_id)
                    except Exception as exc:
                        self.ledger.event(round_id, "CANCEL_ERROR", str(exc))
                    # Sports delay windows can reject cancellation. Keep querying.
                else:
                    raise LiveExecutionError(
                        f"order {order_id} did not reconcile to a terminal state; "
                        f"last error={last_error}"
                    )
            await asyncio.sleep(0.25)

    async def _unwind_excess(
        self,
        round_id: str,
        market: SportsMarket,
        states: tuple[OrderState, OrderState],
        initial_fills: list[VenueFill],
    ) -> list[OrderState]:
        difference = states[0].size_matched - states[1].size_matched
        if difference == ZERO:
            return []
        index = 0 if difference > ZERO else 1
        exposure = abs(difference)
        asset_id = market.asset_ids[index]
        acquisition_price = _weighted_acquisition_price(initial_fills, states[index], asset_id)
        # Include a rough fee reserve in the loss-capped minimum sale price.
        fee_reserve = market.fee_schedule.fee(ONE, acquisition_price)
        minimum_price = acquisition_price + fee_reserve - (self.risk.max_unwind_loss / exposure)
        tick = Decimal("0.01")
        remaining = exposure
        unwind_states: list[OrderState] = []
        for attempt in range(1, self.risk.unwind_attempts + 1):
            books = await self.clob.get_books([asset_id])
            book = books.get(asset_id)
            if book is None or book.best_bid is None:
                self.ledger.event(round_id, "UNWIND_NO_BID", {"attempt": attempt})
                await asyncio.sleep(0.5)
                continue
            tick = book.tick_size or tick
            market_limit = book.best_bid.price - 2 * tick
            limit = max(minimum_price, tick)
            limit = _round_to_tick(limit, tick, buy=False)
            if limit > market_limit or limit >= ONE:
                self.ledger.event(
                    round_id,
                    "UNWIND_PRICE_CAP_BLOCKED",
                    {
                        "attempt": attempt,
                        "minimum_price": str(limit),
                        "best_bid": str(book.best_bid.price),
                    },
                )
                await asyncio.sleep(0.5)
                continue
            signed = await asyncio.to_thread(
                self.gateway.sign_order,
                market,
                asset_id=asset_id,
                side="SELL",
                price=limit,
                size=remaining,
                tick_size=tick,
            )
            raw = await asyncio.to_thread(self.gateway.post_fak, signed)
            submission = OrderSubmission.from_raw(raw)
            self.ledger.record_submission(
                round_id,
                submission,
                asset_id=asset_id,
                side="SELL",
                price=limit,
                size=remaining,
            )
            state = await self._monitor_order(round_id, submission, remaining, market.seconds_delay)
            unwind_states.append(state)
            remaining -= state.size_matched
            if remaining <= ZERO:
                return unwind_states
        self._activate_kill_switch("unwind exhausted with residual exposure")
        return unwind_states

    async def _trades_for_states(
        self, condition_id: str, states: tuple[OrderState, ...] | list[OrderState]
    ) -> list[VenueFill]:
        order_ids = {state.order_id for state in states if state.order_id}
        if not order_ids:
            return []
        # Order states are authoritative for matched size, but complete trade
        # rows are required before calculating cash PnL. Retry index lag and
        # never substitute invented fills for missing venue evidence.
        fills: list[VenueFill] = []
        for _ in range(10):
            raw = await asyncio.to_thread(
                self.gateway.market_trades, condition_id, first_page_only=True
            )
            fills = fills_for_orders(raw, order_ids)
            if fills_cover_states(states, fills):
                return fills
            await asyncio.sleep(0.5)
        self.ledger.event(
            None,
            "TRADE_INDEX_INCOMPLETE",
            {"order_ids": sorted(order_ids)},
        )
        return fills

    async def _finish_round(
        self,
        round_id: str,
        market: SportsMarket,
        states: tuple[OrderState, OrderState],
        unwind_states: list[OrderState],
        fills: list[VenueFill],
    ) -> LiveRoundResult:
        cash_flow = sum(
            (
                fill.notional if fill.side == "SELL" else -fill.notional
                for fill in fills
                if fill.status != "FAILED"
            ),
            ZERO,
        )
        estimated_fees = sum(
            (
                fill.estimated_fee(market.fee_schedule.rate, market.fee_schedule.exponent)
                for fill in fills
                if fill.status != "FAILED"
            ),
            ZERO,
        )
        # Every unwind in one round sells the originally over-filled outcome.
        # Do not guess the asset from a possibly incomplete venue payload.
        unwind_asset_id = (
            market.asset_ids[0]
            if states[0].size_matched > states[1].size_matched
            else market.asset_ids[1]
        )
        total_unwound = sum((state.size_matched for state in unwind_states), ZERO)
        residual_0 = states[0].size_matched
        residual_1 = states[1].size_matched
        if unwind_asset_id == market.asset_ids[0]:
            residual_0 -= total_unwound
        else:
            residual_1 -= total_unwound
        if residual_0 == residual_1 and residual_0 > ZERO:
            status = "HEDGED_OPEN"
        elif residual_0 == residual_1 == ZERO:
            status = "FLAT_UNWOUND" if unwind_states else "NO_FILL"
        else:
            status = "EXPOSED_HALTED"
            self._activate_kill_switch("residual unhedged exposure")
        complete_fills = fills_cover_states((*states, *unwind_states), fills)
        if not complete_fills:
            status = "RECONCILIATION_REQUIRED"
            self._activate_kill_switch("venue trade history is incomplete")
        realized = (
            cash_flow - estimated_fees
            if residual_0 == residual_1 == ZERO and complete_fills
            else None
        )
        positions: list[dict[str, Any]] = []
        try:
            positions = await self.data_api.positions(
                self.gateway.account_address, condition_id=market.condition_id
            )
        except Exception as exc:
            self.ledger.event(round_id, "POSITION_RECONCILIATION_ERROR", str(exc))
        details = {
            "states": [state.raw for state in states],
            "unwind_states": [state.raw for state in unwind_states],
            "residual_shares": [str(residual_0), str(residual_1)],
            "complete_trade_evidence": complete_fills,
            "venue_positions": positions,
            "pnl_note": (
                "cash notionals are venue trades; realized value subtracts estimated "
                "fees unless Data API reports realized_pnl"
            ),
        }
        self.ledger.finish_round(
            round_id,
            status,
            cash_flow_before_fees=cash_flow,
            estimated_fees=estimated_fees,
            estimated_realized_pnl=realized,
            details=details,
        )
        self.ledger.event(round_id, "ROUND_FINISHED", {"status": status})
        order_ids = tuple(state.order_id for state in (*states, *unwind_states) if state.order_id)
        return LiveRoundResult(
            round_id=round_id,
            status=status,
            market_id=market.market_id,
            condition_id=market.condition_id,
            order_ids=order_ids,
            estimated_realized_pnl=realized,
            venue_positions=positions,
        )

    async def _best_effort_market_cancel(self, round_id: str, condition_id: str) -> None:
        try:
            result = await asyncio.to_thread(self.gateway.cancel_market, condition_id)
            self.ledger.event(round_id, "MARKET_CANCEL_RESULT", result)
        except Exception as exc:
            self.ledger.event(round_id, "MARKET_CANCEL_FAILED", str(exc))

    @staticmethod
    def _activate_kill_switch(reason: str) -> None:
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(f"{time.time()}: {reason}\n", encoding="utf-8")


class _Heartbeat:
    def __init__(self, gateway: PolymarketLiveGateway) -> None:
        self.gateway = gateway
        self.healthy = False
        self.heartbeat_id = ""
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        response = await asyncio.to_thread(self.gateway.heartbeat, "")
        self.heartbeat_id = str(response.get("heartbeat_id") or "")
        self.healthy = True
        self._task = asyncio.create_task(self._run(), name="clob-order-heartbeat")

    async def _run(self) -> None:
        try:
            while self.healthy:
                await asyncio.sleep(5)
                response = await asyncio.to_thread(self.gateway.heartbeat, self.heartbeat_id)
                self.heartbeat_id = str(response.get("heartbeat_id") or "")
        except Exception as exc:
            self.healthy = False
            LOGGER.error("Order heartbeat failed: %s", exc)

    async def stop(self) -> None:
        self.healthy = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def protected_buy_limit(book: OrderBook, shares: Decimal) -> Decimal:
    """Worst consumed ask plus one tick, rounded up to a valid tick."""
    remaining = shares
    worst: Decimal | None = None
    for level in book.asks:
        filled = min(remaining, level.size)
        worst = level.price
        remaining -= filled
        if remaining == ZERO:
            break
    if remaining > ZERO or worst is None:
        raise LiveExecutionError("insufficient depth for protected limit")
    tick = book.tick_size or Decimal("0.01")
    return _round_to_tick(worst + tick, tick, buy=True)


def conservative_cost_at_limits(
    market: SportsMarket, shares: Decimal, limit_0: Decimal, limit_1: Decimal
) -> Decimal:
    fee_0 = market.fee_schedule.fee(shares, limit_0)
    fee_1 = market.fee_schedule.fee(shares, limit_1)
    return shares * (limit_0 + limit_1) + fee_0 + fee_1


def _round_to_tick(price: Decimal, tick: Decimal, *, buy: bool) -> Decimal:
    units = price / tick
    rounded = units.to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)
    result = rounded * tick
    if not ZERO < result < ONE:
        raise LiveExecutionError(f"protected price {result} is outside (0, 1)")
    return result


def fills_cover_states(
    states: tuple[OrderState, ...] | list[OrderState], fills: list[VenueFill]
) -> bool:
    """True only when venue trades account for every matched order share."""
    for state in states:
        if state.size_matched == ZERO:
            continue
        covered = sum(
            (
                fill.size
                for fill in fills
                if fill.order_id == state.order_id and fill.status != "FAILED"
            ),
            ZERO,
        )
        if covered < state.size_matched:
            return False
    return True


def _weighted_acquisition_price(
    fills: list[VenueFill], state: OrderState, asset_id: str
) -> Decimal:
    relevant = [
        fill
        for fill in fills
        if fill.order_id == state.order_id and fill.asset_id == asset_id and fill.side == "BUY"
    ]
    size = sum((fill.size for fill in relevant), ZERO)
    if size == ZERO:
        return state.price
    return sum((fill.notional for fill in relevant), ZERO) / size
